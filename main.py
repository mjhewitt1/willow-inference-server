# Profiling
import cProfile as profile
import pstats
# Logging
import os
import sys
import logging
import colorlog
logger = logging.getLogger("infer")
gunicorn_logger = logging.getLogger('gunicorn.error')
logger.handlers = gunicorn_logger.handlers
logger.setLevel(gunicorn_logger.level)

# FastAPI preprocessor
from fastapi import FastAPI, File, Form, UploadFile, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from typing import List, Optional, Tuple
import types
import random
import datetime
import numpy as np
from PIL import Image
import warnings
warnings.simplefilter(action='ignore')
from transformers import AutoImageProcessor, SpeechT5Processor, SpeechT5ForTextToSpeech, SpeechT5HifiGan
from datasets import load_dataset
import tritonclient.grpc as grpcclient
import json
import io
import re
import math
import functools

# WebRTC
import asyncio
#import uvloop
#asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
import uuid

from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription, RTCRtpReceiver
from aiortc.rtp import RtcpByePacket
from media import MediaRecorderLite

pcs = set()

# Whisper
import ctranslate2
import librosa
import transformers
import datetime

import torch
# Import audio stuff adapted from ref Whisper implementation
from audio import log_mel_spectrogram, pad_or_trim, chunk_iter, find_longest_common_sequence

# Monkey patch aiortc
# sender.replaceTrack(null) sends a RtcpByePacket which we want to ignore
# in this case and keep connection open. XXX: Are there other cases we want to close?
old_handle_rtcp_packet = RTCRtpReceiver._handle_rtcp_packet
async def new_handle_rtcp_packet(self, packet):
    if isinstance(packet, RtcpByePacket):
        return
    return old_handle_rtcp_packet(self, packet)
RTCRtpReceiver._handle_rtcp_packet = new_handle_rtcp_packet
#logging.basicConfig(level=logger.DEBUG) #very useful debugging aiortc issues

# Monkey patch aiortc to control ephemeral ports
local_ports = list(range(10000, 10000+300)) # Allowed ephemeral port range
loop = asyncio.get_event_loop()
old_create_datagram_endpoint = loop.create_datagram_endpoint
async def create_datagram_endpoint(self, protocol_factory,
    local_addr: Tuple[str, int] = None,
    **kwargs,
):
    #if port is specified just use it
    if local_addr and local_addr[1]:
        return await old_create_datagram_endpoint(protocol_factory, local_addr=local_addr, **kwargs)
    if local_addr is None: 
        return await old_create_datagram_endpoint(protocol_factory, local_addr=None, **kwargs)
    #if port is not specified make it use our range
    ports = list(local_ports)
    random.shuffle(ports)
    for port in ports:
        try:
            ret = await old_create_datagram_endpoint(
                protocol_factory, local_addr=(local_addr[0], port), **kwargs
            )
            logger.debug(f'create_datagram_endpoint chose port {port}')
            return ret
        except OSError as exc:
            if port == ports[-1]:
                # this was the last port, give up
                raise exc
    raise ValueError("local_ports must not be empty")
loop.create_datagram_endpoint = types.MethodType(create_datagram_endpoint, loop)

# default return language
return_language = "en"

# default beam_size - 5 is lib default, 1 for greedy
beam_size = 2

# default beam size for longer transcriptions
long_beam_size = 5
# Audio duration in ms to activate "long" mode
long_beam_size_threshold = 12000

# model threads
model_threads = os.environ.get('MODEL_THREADS', 10)

# Make sure model_threads is int
model_threads = int(model_threads)

# Try CUDA
device = "cuda" if torch.cuda.is_available() else "cpu"

if device == "cuda":
    # Get CUDA device capability
    device_capability = torch.cuda.get_device_capability()
    device_capability = functools.reduce(lambda sub, ele: sub * 10 + ele, device_capability)
    logger.info(f'CUDA Device capability {device_capability}')

    # Use int8_float16 on Turing or higher - int8 on anything else
    if device_capability >= 70:
        compute_type = "int8_float16"
    else:
        compute_type = "int8"

    # Set ctranslate device index based on number of supported devices
    num_devices = torch.cuda.device_count()
    logger.info(f'Detected {num_devices} CUDA devices')
    device_index = [*range(0, num_devices, 1)]
else:
    num_cpu_cores = os.cpu_count()
    compute_type = "int8"
    # Just kind of made these numbers up - needs testing
    intra_threads = num_cpu_cores // 2
    model_threads = num_cpu_cores // 2
    logger.info(f'CUDA not found - using CPU with {num_cpu_cores} cores')

# Turn up log_level for ctranslate2
#ctranslate2.set_log_level(logger.DEBUG)

# Load processor from transformers
processor = transformers.WhisperProcessor.from_pretrained("openai/whisper-large-v2")

# Show supported compute types
supported_compute_types = str(ctranslate2.get_supported_compute_types(device))
logger.info(f'Supported ctranslate compute types for device {device} are {supported_compute_types} - using configured {compute_type}')

# Load all models - thanks for quantization ctranslate2
if device == "cuda":
    whisper_model_base = ctranslate2.models.Whisper('models/openai-whisper-base', device=device, compute_type=compute_type, device_index=device_index, inter_threads=model_threads)
    whisper_model_medium = ctranslate2.models.Whisper('models/openai-whisper-medium', device=device, compute_type=compute_type, device_index=device_index, inter_threads=model_threads)
    whisper_model_large = ctranslate2.models.Whisper('models/openai-whisper-large-v2', device=device, compute_type=compute_type, device_index=device_index, inter_threads=model_threads)
    whisper_model_default = 'large'
else:
    whisper_model_base = ctranslate2.models.Whisper('models/openai-whisper-base', device=device, compute_type=compute_type, inter_threads=model_threads, intra_threads=intra_threads)
    whisper_model_medium = ctranslate2.models.Whisper('models/openai-whisper-medium', device=device, compute_type=compute_type, inter_threads=model_threads, intra_threads=intra_threads)
    whisper_model_large = ctranslate2.models.Whisper('models/openai-whisper-large-v2', device=device, compute_type=compute_type, inter_threads=model_threads, intra_threads=intra_threads)
    whisper_model_default = 'base'

# Default detect language?
detect_language = False

# Triton
triton_url = os.environ.get('triton_url', 'hw0-mke.tovera.com:18001')
triton_model = os.environ.get('triton_model', 'medvit-trt-fp32')

def warm_models():
    logger.info("Warming models...")
    for x in range(5):
        if whisper_model_base is not None:
            do_whisper("3sec.flac", "base", beam_size, "transcribe", False, "en")
        if whisper_model_medium is not None:
            do_whisper("3sec.flac", "medium", beam_size, "transcribe", False, "en")
        if whisper_model_large is not None:
            do_whisper("3sec.flac", "large", beam_size, "transcribe", False, "en")

def do_translate(features, language, beam_size=beam_size):
    # Set task in token format for processor
    task = 'translate'
    processor_task = f'<|{task}|>'

    # Describe the task in the prompt.
    # See the prompt format in https://github.com/openai/whisper.
    prompt = processor.tokenizer.convert_tokens_to_ids(
        [
            "<|startoftranscript|>",
            language,
            processor_task,
            "<|notimestamps|>",  # Remove this token to generate timestamps.
        ]
    )

    # Run generation for the 30-second window.
    time_start = datetime.datetime.now()
    results = whisper_model_large.generate(features, [prompt], beam_size=beam_size)
    time_end = datetime.datetime.now()
    infer_time = time_end - time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('Translate inference took ' + str(infer_time_milliseconds) + ' ms')
    results = processor.decode(results[0].sequences_ids[0])
    logger.debug(results)

    return results

def do_whisper(audio_file, model, beam_size, task, detect_language, return_language):
    if model != "large" and detect_language == True:
        logger.warning(f'WHISPER: Language detection requested but not supported on model {model} - overriding with large')
        model = "large"
        beam_size = 5
    # Point to model object depending on passed model string
    if model == "large":
        whisper_model = whisper_model_large
    elif model == "medium":
        whisper_model = whisper_model_medium
    elif model == "base":
        whisper_model = whisper_model_base

    processor_task = f'<|{task}|>'
    first_time_start = datetime.datetime.now()

    # Whisper STEP 1 - load audio and extract features
    audio, audio_sr = librosa.load(audio_file, sr=16000, mono=True)
    audio_duration = librosa.get_duration(y=audio, sr=audio_sr) * 1000
    audio_duration = int(audio_duration)
    if audio_duration >= long_beam_size_threshold:
        logger.debug(f'WHISPER: Audio duration is {audio_duration} ms - activating long mode')
        beam_size = long_beam_size
    use_chunking = False
    if audio_duration > 30*1000:
        logger.debug(f'WHISPER: Audio duration is > 30s - activating chunking')
        use_chunking = True

    time_end = datetime.datetime.now()
    infer_time = time_end - first_time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('WHISPER: Loading audio took ' + str(infer_time_milliseconds) + ' ms')

    time_start = datetime.datetime.now()
    if use_chunking:
        chunks = []
        strides = []
        for chunk, stride in chunk_iter(audio):
            chunk = pad_or_trim(chunk)
            chunks.append(log_mel_spectrogram(chunk).numpy())
            strides.append(stride)
        mel_features = np.stack(chunks)
        batch_size = len(chunks)
    else:
        mel_audio = pad_or_trim(audio)
        mel_features = log_mel_spectrogram(mel_audio).numpy()
        # Ref Whisper returns shape (80, 3000) but model expects (1, 80, 3000)
        mel_features = np.expand_dims(mel_features, axis=0)
        batch_size = 1
    features = ctranslate2.StorageView.from_array(mel_features)

    time_end = datetime.datetime.now()
    infer_time = time_end - time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('WHISPER: Feature extraction took ' + str(infer_time_milliseconds) + ' ms')

    # Whisper STEP 2 - optionally actually detect the language or default to en
    time_start = datetime.datetime.now()
    if detect_language:
        results = whisper_model.detect_language(features)
        language, probability = results[0][0]
        logger.debug("WHISPER: Detected language %s with probability %f" % (language, probability))

    else:
        logger.debug('WHISPER: Hardcoding language to en')
        # Hardcode language
        language = '<|en|>'

    # Describe the task in the prompt.
    # See the prompt format in https://github.com/openai/whisper.
    prompt = processor.tokenizer.convert_tokens_to_ids(
        [
            "<|startoftranscript|>",
            language,
            processor_task,
            "<|notimestamps|>",  # Remove this token to generate timestamps.
        ]
    )
    time_end = datetime.datetime.now()
    infer_time = time_end - time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('WHISPER: Language detection took ' + str(infer_time_milliseconds) + ' ms')

    # Whisper STEP 3 - run model
    time_start = datetime.datetime.now()
    logger.debug(f'WHISPER: Using model {model} with beam size {beam_size}')
    results = whisper_model.generate(features, [prompt]*batch_size, beam_size=beam_size, return_scores=False)
    time_end = datetime.datetime.now()
    infer_time = time_end - time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('WHISPER: Model took ' + str(infer_time_milliseconds) + ' ms')
    
    time_start = datetime.datetime.now()
    if use_chunking:
        assert strides, 'strides needed to compute final tokens when chunking'
        tokens = [(results[i].sequences_ids[0], strides[i]) for i in range(batch_size)]
        tokens = find_longest_common_sequence(tokens, processor.tokenizer)
    else:
        tokens = results[0].sequences_ids[0]
    results = processor.decode(tokens)
    time_end = datetime.datetime.now()
    infer_time = time_end - time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('WHISPER: Decode took ' + str(infer_time_milliseconds) + ' ms')
    logger.debug('WHISPER: ASR transcript: ' + results)

    # Strip out token stuff
    pattern = re.compile("[A-Za-z0-9]+", )
    language = pattern.findall(language)[0]

    if not language == return_language:
        logger.debug(f'WHISPER: Detected non-preferred language {language}, translating to {return_language}')
        translation = do_translate(features, language, beam_size=beam_size)
        translation = translation.strip()
        logger.debug('WHISPER: ASR translation: ' + translation)
    else:
        translation = None

    # Remove trailing and leading spaces
    results = results.strip()

    time_end = datetime.datetime.now()
    infer_time = time_end - first_time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('WHISPER: Inference took ' + str(infer_time_milliseconds) + ' ms')
    infer_speedup = math.floor(audio_duration / infer_time_milliseconds)
    logger.debug('WHISPER: Inference speedup: ' + str(infer_speedup) + 'x')

    return language, results, infer_time_milliseconds, translation, infer_speedup, audio_duration

# TTS
import soundfile as sf
import torchaudio

tts_speaker_embeddings = {
    "BDL": "spkemb/cmu_us_bdl_arctic-wav-arctic_a0009.npy",
    "CLB": "spkemb/cmu_us_clb_arctic-wav-arctic_a0144.npy",
    "KSP": "spkemb/cmu_us_ksp_arctic-wav-arctic_b0087.npy",
    "RMS": "spkemb/cmu_us_rms_arctic-wav-arctic_b0353.npy",
    "SLT": "spkemb/cmu_us_slt_arctic-wav-arctic_a0508.npy",
}

# US female
tts_default_speaker = "CLB"

tts_processor = SpeechT5Processor.from_pretrained("microsoft/speecht5_tts")
tts_model = SpeechT5ForTextToSpeech.from_pretrained("microsoft/speecht5_tts").to(device=device)
# tts_embeddings_dataset = load_dataset("Matthijs/cmu-arctic-xvectors", split="validation")
# tts_speaker_embeddings = torch.tensor(tts_embeddings_dataset[7306]["xvector"]).unsqueeze(0)
tts_vocoder = SpeechT5HifiGan.from_pretrained("microsoft/speecht5_hifigan").to(device=device)

def do_tts(text, speaker = tts_default_speaker):
    logger.debug(f'TTS: Got request for text {text} with speaker {speaker}')

    # Load speaker embedding
    time_start = datetime.datetime.now()
    speaker = speaker.upper()
    speaker_embedding = np.load(tts_speaker_embeddings[speaker[:3]])
    speaker_embedding = torch.tensor(speaker_embedding).unsqueeze(0).to(device=device)
    time_end = datetime.datetime.now()
    infer_time = time_end - time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('TTS: Loading speaker embedding took ' + str(infer_time_milliseconds) + ' ms')

    # Get inputs
    time_start = datetime.datetime.now()
    inputs = tts_processor(text=text, return_tensors="pt").to(device=device)
    time_end = datetime.datetime.now()
    infer_time = time_end - time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('TTS: Getting inputs took ' + str(infer_time_milliseconds) + ' ms')

    # Generate spectrogram - SLOW
    time_start = datetime.datetime.now()
    spectrogram = tts_model.generate_speech(inputs["input_ids"], speaker_embedding).to(device=device)
    time_end = datetime.datetime.now()
    infer_time = time_end - time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('TTS: Generating spectrogram took ' + str(infer_time_milliseconds) + ' ms')
    
    # Generate audio - SLOW
    time_start = datetime.datetime.now()
    audio = tts_model.generate_speech(inputs["input_ids"], speaker_embedding, vocoder=tts_vocoder).to(device=device)
    time_end = datetime.datetime.now()
    infer_time = time_end - time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('TTS: Generating audio took ' + str(infer_time_milliseconds) + ' ms')

    # Setup access to file and pass it back to calling function
    time_start = datetime.datetime.now()

    # If we're not running on CPU copy audio to CPU so we can write it out
    if device != "cpu":
        audio = audio.cpu()

    file = io.BytesIO()
    sf.write(file, audio.numpy(), samplerate=16000, format='FLAC')
    file.seek(0)
    time_end = datetime.datetime.now()
    infer_time = time_end - time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('TTS: Generating file took ' + str(infer_time_milliseconds) + ' ms')

    return file

# transformers
def get_transform(img):
    processor = AutoImageProcessor.from_pretrained('preprocessor_config.json')
    image = Image.open(img)
    if not image.mode == "RGB":
        image = image.convert("RGB")
    processed_image = processor(image, return_tensors='np').pixel_values
    return processed_image

def do_infer(img, triton_model):
    # Set up Triton GRPC inference client
    client = grpcclient.InferenceServerClient(url=triton_url, verbose=False, ssl=False)

    # Use transformers
    input = get_transform(img)

    # Hack shape for now - hopefully we can get it dynamically from transformers or something
    shape=(1, 3, 224, 224)

    inputs = grpcclient.InferInput('input__0', shape, datatype='FP32')
    inputs.set_data_from_numpy(input)
    outputs = grpcclient.InferRequestedOutput('output__0', class_count=10)

    # Send for inference
    logger.debug('Doing triton inference with model ' + triton_model + " and url " + triton_url)
    time_start = datetime.datetime.now()
    results = client.infer(model_name=triton_model, inputs=[inputs], outputs=[outputs])

    time_end = datetime.datetime.now()
    infer_time = time_end - time_start
    infer_time_milliseconds = infer_time.total_seconds() * 1000
    logger.debug('Inference took ' + str(infer_time_milliseconds) + ' ms')

    inference_output = results.as_numpy('output__0')

    # Display and return full results
    logger.debug(str(inference_output))
    inference_output_dict = dict(enumerate(inference_output.flatten(), 1))
    logger.debug(inference_output_dict)
    return inference_output_dict, infer_time_milliseconds

# Function for WebRTC handling
async def rtc_offer(request, model, beam_size, task, detect_language, return_language):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    pc_id = "PeerConnection(%s)" % uuid.uuid4()
    pcs.add(pc)

    logger.debug(f'RTC: Created for {request.client.host}')

    @pc.on("datachannel")
    def on_datachannel(channel):
        @channel.on("message")
        def on_message(message):
            logger.debug("RTC DC: message: " + message)
            if isinstance(message, str) and message.startswith("ping"):
                channel.send("pong" + message[4:])
            if isinstance(message, str) and message.startswith("start"):
                logger.debug("RTC DC: Recording started")
                global recorder
                logger.debug(f'RTC DC: Recording with track {global_track}')
                recorder = MediaRecorderLite()
                recorder.addTrack(global_track)
                recorder.start()
                channel.send('ASR Recording - start talking and press stop when done')
            if isinstance(message, str) and message.startswith("stop"):
                try:
                    action_list = message.split(":")
                    logger.debug(f'RTC DC: Got action list {action_list}')
                except:
                    logger.debug('RTC DC: Failed to get action list - setting to none')
                    action_list = None
                if action_list[1] is not None:
                    model = action_list[1]
                    logger.debug(f'RTC DC: Got DC provided model {model}')
                else:
                    logger.debug('RTC DC: Failed getting model from DC')
                if action_list[2] is not None:
                    beam_size = int(action_list[2])
                    logger.debug(f'RTC DC: Got DC provided beam size {beam_size}')
                else:
                    logger.debug('RTC DC: Failed getting beam size from DC')
                if action_list[3] is not None:
                    detect_language = eval(action_list[3])
                    logger.debug(f'RTC DC: Got DC provided detect language {detect_language}')
                else:
                    logger.debug('RTC DC: Failed getting detect language from DC')
                logger.debug(f'RTC DC: Debug vars {model} {beam_size} {detect_language}')
                logger.debug("RTC DC: Recording stopped")
                time_start_base = datetime.datetime.now()
                time_end = datetime.datetime.now()
                infer_time = time_end - time_start_base
                infer_time_milliseconds = infer_time.total_seconds() * 1000
                recorder.stop()
                logger.debug('RTC DC: Recorder stop took ' + str(infer_time_milliseconds) + ' ms')
                # Tell client what we are doing
                channel.send(f'Doing ASR with model {model} beam size {beam_size} detect language {detect_language} - please wait')
                # Finally call Whisper
                recorder.file.seek(0)
                language, results, infer_time, translation, infer_speedup, audio_duration = do_whisper(recorder.file, model, beam_size, task, detect_language, return_language)
                logger.debug("RTC DC: " + results)
                channel.send('ASR Transcript: ' + results)
                if translation:
                    channel.send(f'ASR Translation from {language}:  {translation}')
                infer_time = str(infer_time)
                audio_duration = str(audio_duration)
                infer_speedup = str(infer_speedup)
                channel.send(f'ASR Infer time: {infer_time} ms')
                channel.send(f'ASR Audio Duration: {audio_duration} ms')
                channel.send(f'ASR Speedup: {infer_speedup}x faster than realtime')

                #del recorder

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        logger.debug(f'RTC: Connection state is {pc.connectionState}')
        if pc.connectionState == "failed" or pc.connectionState == "closed":
            try:
                await recorder.stop()
            except:
                pass
            else:
                logger.debug("RTC: Recording stopped")
            await pc.close()
            pcs.discard(pc)
            #XXX: close recorders?
            logger.debug("RTC: Connection ended")

    @pc.on("track")
    def on_track(track):
        logger.debug(f'RTC: Track received {track.kind}')
        if track.kind == "audio":
            logger.debug("RTC: Setting global track")
            global global_track
            global_track = track

        @track.on("ended")
        async def on_ended():
            logger.debug(f'RTC: Track ended {track.kind}')

    # handle offer
    await pc.setRemoteDescription(offer)

    # send answer
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}

# Warm models
warm_models()

app = FastAPI()

# BROKEN: Disable middleware for now
#@app.middleware("http")
async def do_auth(request: Request, response: Response, call_next):
    api_key = os.environ.get('API_KEY')
    auth_header = request.headers.get('X-API-Key')

    logger.debug(f'FASTAPI: Got API key {api_key}')
    logger.debug(f'FASTAPI: Got API key header {auth_header}')

    if api_key is not None:
        if auth_header == api_key:
            logger.debug(f'FASTAPI: Request is authorized')
        else:
            logger.debug(f'FASTAPI: Request is unauthorized')
            response =  {"result": "no_auth"}
            response.status_code = status.HTTP_401_UNAUTHORIZED
            return response
    else:
        # If we got here we either don't have auth or it's good
        response = await call_next(request)
        return response

# Mount static dir to serve files for aiortc client
app.mount("/rtc", StaticFiles(directory="rtc", html = True), name="rtc_files")

@app.on_event('shutdown')
def shutdown_event():
    logger.info("FASTAPI: Got shutdown - we should properly handle in progress recording")

@app.get("/ping")
async def ping():
    return {"message": "Pong"}

@app.post("/api/infer")
async def infer(request: Request, file: UploadFile, response: Response, model: Optional[str] = triton_model):
    # Setup access to file
    audio_file = io.BytesIO(await file.read())
    response, infer_time = do_infer(img, model)
    final_response = {"infer_time": infer_time, "results": [response]}
    json_compatible_item_data = jsonable_encoder(final_response)
    return JSONResponse(content=json_compatible_item_data)

@app.post("/api/rtc/asr")
async def rtc_asr(request: Request, response: Response, model: Optional[str] = whisper_model_default, task: Optional[str] = "transcribe", detect_language: Optional[bool] = detect_language, return_language: Optional[str] = return_language, beam_size: Optional[int] = beam_size):
    response = await rtc_offer(request, model, beam_size, task, detect_language, return_language)
    return JSONResponse(content=response)

@app.post("/api/asr")
async def asr(request: Request, audio_file: UploadFile, response: Response, model: Optional[str] = whisper_model_default, task: Optional[str] = "transcribe", detect_language: Optional[bool] = detect_language, return_language: Optional[str] = return_language, beam_size: Optional[int] = beam_size):
    #prof = profile.Profile()
    #prof.enable()

    logger.debug(f"FASTAPI: Got ASR request for model {model} beam size {beam_size} language detection {detect_language}")
    # Setup access to file
    audio_file = io.BytesIO(await audio_file.read())
    # Do Whisper
    language, results, infer_time, translation, infer_speedup, audio_duration = do_whisper(audio_file, model, beam_size, task, detect_language, return_language)

    # Create final response
    final_response = {"infer_time": infer_time, "infer_speedup": infer_speedup, "audio_duration": audio_duration, "language": language, "text": results}

    # Handle translation in one response
    if translation:
        final_response['translation']=translation

    json_compatible_item_data = jsonable_encoder(final_response)
    #prof.disable()
    # print profiling output
    #stats = pstats.Stats(prof).strip_dirs().sort_stats("cumtime")
    #stats.print_stats(10) # top 10 rows
    return JSONResponse(content=json_compatible_item_data)

@app.get("/api/tts")
async def tts(text: str, speaker: Optional[str] = tts_default_speaker):
    # Do TTS
    response = do_tts(text, speaker)
    return StreamingResponse(response, media_type="audio/flac")

@app.post("/api/sts")
async def sts(request: Request, audio_file: UploadFile, response: Response, model: Optional[str] = whisper_model_default, task: Optional[str] = "transcribe", detect_language: Optional[bool] = detect_language, return_language: Optional[str] = return_language, beam_size: Optional[int] = beam_size, speaker: Optional[str] = tts_default_speaker):
    #prof = profile.Profile()
    #prof.enable()

    logger.debug(f"FASTAPI: Got STS request for model {model} beam size {beam_size} language detection {detect_language}")
    # Setup access to file
    audio_file = io.BytesIO(await audio_file.read())
    # Do Whisper
    language, results, infer_time, translation, infer_speedup, audio_duration = do_whisper(audio_file, model, beam_size, task, detect_language, return_language)

    # Do TTS
    response = do_tts(results, speaker)
    return StreamingResponse(response, media_type="audio/flac")