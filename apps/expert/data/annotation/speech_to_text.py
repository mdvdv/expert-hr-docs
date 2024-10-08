from __future__ import annotations

import re
from math import exp
from os import PathLike
from typing import Dict, List, Tuple

import torch

from faster_whisper import WhisperModel


def transcribe_video(
    video_path: str | PathLike,
    lang: str = "en",
    model: str = "server",
    device: torch.device | None = None,
) -> Dict:
    """Speech recognition module from video.

    Args:
        video_path (str | Pathlike): Path to the local video file.
        lang (str, optional): Language for speech recognition ['ru', 'en']. Defaults to 'en'.
        model (str, optional): Model configuration for speech recognition ['server', 'local']. Defaults to 'server'.
        device (torch.device | None, optional): Device type on local machine (GPU recommended). Defaults to None.

    Raises:
        NotImplementedError: If 'lang' is not equal to 'en' or 'ru'.
        NotImplementedError: If 'model' is not equal to 'server' or 'local'.
    """
    if lang not in ["en", "ru"]:
        raise NotImplementedError("'lang' must be 'en' or 'ru'.")
    if model not in ["server", "local"]:
        raise NotImplementedError("'model' must be 'server' or 'local'.")

    _device = torch.device("cpu")
    if device is not None:
        _device = device

    if model == "server":
        model = WhisperModel("medium", device=_device.type, compute_type="float32")
    elif model == "local":
        model = WhisperModel("small", device=_device.type, compute_type="float32")

    transcription = {
        "stamps": [],
        "full_text": "",
        "sentences": [],
        "transcribed_text": [],
    }

    pattern = re.compile("[\.!?]")

    cur_time_start = 0
    cur_sentence = []
    segments, info = model.transcribe(video_path, language="ru", word_timestamps=True)
    for segment in segments:
        if len(cur_sentence) == 0:
            cur_time_start = segment.start

        cur_sentence.append(segment.text)

        if pattern.match(cur_sentence[-1][-1]):
            transcription["sentences"].append(
                {
                    "time_start": cur_time_start,
                    "text": "".join(cur_sentence).strip(),
                    "time_end": segment.end,
                }
            )
            cur_sentence = []

        transcription["full_text"] += segment.text
        transcription["stamps"].append((segment.start, segment.end))
        transcription["transcribed_text"].append(
            {
                "text": segment.text.strip(),
                "start": segment.start,
                "end": segment.end,
                "confidence": round(exp(segment.avg_logprob), 3),
            }
        )
    transcription["full_text"] = transcription["full_text"].strip()

    return transcription


def get_all_words(transcribation: Dict) -> Tuple[List, str]:
    """Get all stamps with words from the transcribed text.

    Args:
        transcribation (Dict): Speech recognition module results.
    """
    full_text = transcribation["text"]
    all_words = []
    for segment in transcribation["segments"]:
        for word in segment["words"]:
            all_words.append(word)

    return all_words, full_text


def get_phrases(all_words: list, duration: int = 10) -> list:
    """Split transcribed text into segments of a fixed length.

    Args:
        all_words (List): All stamps with words from the transcribed text.
        duration (int, optional): Length of intervals for extracting phrases from speech. Defaults to 10.
    """
    phrases = []

    assert len(all_words) > 1, "Not enough words in text."

    while all_words:
        init_elem = all_words.pop(0)
        phrase = init_elem["text"]
        time_left = duration - (init_elem["end"] - init_elem["start"])
        end_time = init_elem["end"]
        if time_left < 0:
            phrases.append(
                {
                    "time_start": init_elem["start"],
                    "text": phrase,
                    "time_end": init_elem["end"],
                }
            )
            time_left -= init_elem["end"] - end_time
            end_time = init_elem["end"]
            continue
        while time_left > 0 and all_words:
            elem = all_words.pop(0)
            phrase = phrase + " " + elem["text"]
            time_left -= elem["end"] - end_time
            end_time = elem["end"]
        else:
            phrases.append(
                {
                    "time_start": init_elem["start"],
                    "text": phrase,
                    "time_end": elem["end"],
                }
            )

    return phrases


def get_sentences(all_words: list):
    pattern = re.compile("[\.!?]")
    sentences = []
    current_sentence = []
    for elem in all_words:
        if (
            pattern.match(elem["text"][-1])
            and len(current_sentence) > 0
            and len(current_sentence) > 3
        ):
            current_sentence.append(elem["text"])
            sentences.append(
                {
                    "time_start": current_sentence[0],
                    "text": " ".join(current_sentence[1:]),
                    "time_end": elem["end"],
                }
            )
            current_sentence = []
        elif not pattern.match(elem["text"][-1]) and len(current_sentence) == 0:
            current_sentence.append(elem["start"])
            current_sentence.append(elem["text"])
        else:
            if len(current_sentence) == 0:
                current_sentence.append(elem["start"])
            current_sentence.append(elem["text"])
    return sentences


def between_timestamps(all_words: List, start: float, end: float) -> str:
    """Get phrase between specific timestamps (start, finish) in seconds.
    Find closest left index for start stamp and closest right index for end.

    Args:
        all_words (List): All stamps with words from the transcribed text.
        start (float): Start timestamp of the interval (in seconds).
        end (float): End timestamp of the interval (in seconds).

    Returns:
        str: Phrase between timestamps.
    """

    def _binary_search(stamps: List, val: float):
        """Inner function to obtain clossest indexes."""
        lowIdx, highIdx = 0, len(stamps) - 1
        while highIdx > lowIdx:
            idx = (highIdx + lowIdx) // 2
            elem = stamps[idx]
            if stamps[lowIdx] == val:
                return [lowIdx, lowIdx]
            elif elem == val:
                return [idx, idx]
            elif elem > val:
                if highIdx == idx:
                    return [lowIdx, highIdx]
                highIdx = idx
            else:
                if lowIdx == idx:
                    return [lowIdx, highIdx]
                lowIdx = idx
        return [lowIdx, highIdx]

    assert start >= 0, "Innapropriate start stamp (negative value)"

    starts = [elem["start"] for elem in all_words]
    ends = [elem["end"] for elem in all_words]
    start_idx = min(_binary_search(starts, start))
    end_idx = max(_binary_search(ends, end))
    # to get the last word
    if end > all_words[-1]["end"]:
        end_idx += 1
    words = [elem["text"] for elem in all_words[start_idx:end_idx]]
    return " ".join(words)
