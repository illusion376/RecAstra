import os
import re
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File
from nexara import Nexara

load_dotenv()

app = FastAPI()

client = Nexara(api_key=("nx-c4irCkjHTU05CR5RIxj1rjiD")) #скрыть при заливке!!


def format_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def split_into_sentences_with_time(text: str, start: float, end: float) -> list[dict]:
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    sentences = [s for s in sentences if s]

    total_chars = sum(len(s) for s in sentences) or 1
    duration = end - start

    result = []
    elapsed = 0.0
    for s in sentences:
        t = start + (elapsed / total_chars) * duration
        result.append({"time": format_time(t), "text": s})
        elapsed += len(s)
    return result


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    audio_bytes = await file.read()
    result = client.transcriptions.create(file=audio_bytes, task="diarize")

    segments = []
    for s in result.segments:
        for piece in split_into_sentences_with_time(s.text, s.start, s.end):
            segments.append({"speaker": s.speaker, **piece})

    return {"segments": segments}