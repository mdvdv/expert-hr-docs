from __future__ import annotations

import json
import os
from os import PathLike
from typing import List, Tuple

import torch

from apps.expert.core.aggression.audio_aggression.audio_analysis import (
    AudioAggression,
)
from apps.expert.core.aggression.text_aggression.text_analysis import TextAggression
from apps.expert.core.aggression.video_aggression.video_analysis import (
    VideoAggression,
)
from apps.expert.data.annotation.speech_to_text import get_phrases


class AggressionDetector:
    """AggressionDetector.

    Args:
        video_path (str | PathLike): Path to the local video file.
        features_path (str | PathLike): Path to the result of feature extraction module.
        diarization_path (str | PathLike): Path to the result of diarization module.
        transcription_path (str | PathLike): Path to the result of speech recognition module.
        lang (str, optional): Speech language for text processing ['ru', 'en']. Defaults to 'en'.
        device (torch.device | None, optional): Device type on local machine (GPU recommended). Defaults to None.
        duration: Length of intervals for extracting features. Defaults to 10.
        sr (int, optional): Sample rate. Defaults to 16000.
        static_image_mode (int, optional): Whether to treat the input images as a batch
            of static and possibly unrelated images or a video stream. Defaults to False.
        max_num_faces (int, optional): Maximum number of faces to detect. Defaults to 1.
        refine_landmarks(bool, optional): Whether to further refine the landmark coordinates
            around the eyes, lips and output additional landmarks around the irises. Defaults to True.
        min_detection_confidence(float, optional): Minimum confidence value ([0.0, 1.0]) for face
            detection to be considered successful. Defaults to 0.5.
        min_tracking_confidence (float, optional): Minimum confidence value ([0.0, 1.0]) for the
            face landmarks to be considered tracked successfully. Defaults to 0.5.
        output_dir (str | Pathlike | None, optional): Path to the folder for saving results. Defaults to None.
        return_path (bool): Flag to define the return mode for get_congruence. True is for path, False is for dict. Defaults to False

    Returns:
        Tuple[str, str]: Paths to divided and aggregated characteristics of aggression.

    Raises:
        NotImplementedError: If 'lang' is not equal to 'en' or 'ru'.

    Example:
        >>> import torch
        >>> detector = AggressionDetector(
        >>>     video_path="test_video.mp4",
        >>>     features_path="temp/test_video/features.json",
        >>>     diarization_path="temp/test_video/diarization.json",
        >>>     transcription_path="temp/test_video/transcription.json",
        >>>     lang="ru",
        >>>     device=torch.device("cuda")
        >>> )
        >>> detector.get_aggression()
    """

    def __init__(
        self,
        video_path: str | PathLike,
        features_path: str | PathLike,
        diarization_path: str | PathLike,
        transcription_path: str | PathLike,
        lang: str = "en",
        device: torch.device | None = None,
        duration: int = 10,
        sr: int = 16000,
        static_image_mode: bool = False,
        max_num_faces: int = 1,
        refine_landmarks: bool = True,
        min_detection_confidence: float = 0.5,
        min_tracking_confidence: float = 0.5,
        output_dir: str | PathLike | None = None,
        return_path: bool = False,
    ) -> None:
        if lang not in ["en", "ru"]:
            raise NotImplementedError("'lang' must be 'en' or 'ru'.")

        self.lang = lang
        self.video_path = video_path
        self.features_path = features_path
        self.duration = duration
        self.sr = sr
        self.static_image_mode = static_image_mode
        self.max_num_faces = max_num_faces
        self.refine_landmarks = refine_landmarks
        self.min_detection_confidence = min_detection_confidence
        self.min_tracking_confidence = min_tracking_confidence
        self._device = torch.device("cpu")

        if device is not None:
            self._device = device

        with open(diarization_path, "r") as file:
            self.stamps = json.load(file)
        with open(transcription_path, "r") as file:
            self.phrases = get_phrases(json.load(file), duration=self.duration)

        if output_dir is not None:
            self.temp_path = output_dir
        else:
            basename = os.path.splitext(os.path.basename(video_path))[0]
            self.temp_path = os.path.join("temp", basename)
        if not os.path.exists(self.temp_path):
            os.makedirs(self.temp_path)

        self.div_agg = {}
        self.full_agg = {}

        self.return_path = return_path

    @property
    def device(self) -> torch.device:
        """Check the device type.

        Returns:
            torch.device: Device type on local machine.
        """
        return self._device

    def get_video_state(self) -> Tuple[List, List, str]:
        """Extract aggression markers from video channel.

        Returns:
            Tuple[List, List, str]: Lists of divided and aggregated video markers and key to the speaker's speech.
        """
        video_model = VideoAggression(
            video_path=self.video_path,
            features_path=self.features_path,
            device=self._device,
            static_image_mode=self.static_image_mode,
            max_num_faces=self.max_num_faces,
            refine_landmarks=self.refine_landmarks,
            min_detection_confidence=self.min_detection_confidence,
            min_tracking_confidence=self.min_tracking_confidence,
        )
        div_vid_agg, full_vid_agg = video_model.get_report()

        return div_vid_agg, full_vid_agg

    def get_audio_state(self, stamps: List[List]) -> Tuple[List, List]:
        """Extract aggression markers from audio channel.

        Returns:
            Tuple[List, List]: Lists of divided and aggregated audio markers.
        """
        audio_model = AudioAggression(
            audio=self.video_path,
            stamps=stamps,
            duration=self.duration,
            sr=self.sr,
        )
        div_aud_agg, full_aud_agg = audio_model.get_report()

        return div_aud_agg, full_aud_agg

    def get_text_state(self, fragments) -> Tuple[List, List]:
        """Extract aggression markers from text channel.

        Returns:
            Tuple[List, List]: Lists of divided and aggregated text markers.
        """
        text_model = TextAggression(
            fragments=fragments, lang=self.lang, device=self._device
        )
        div_text_agg, full_text_agg = text_model.get_report()

        return div_text_agg, full_text_agg

    def get_aggression(self) -> Tuple[str, str]:
        div_vid_agg, full_vid_agg = self.get_video_state()
        div_aud_agg, full_aud_agg = self.get_audio_state(stamps=self.stamps)

        fragments = []
        for phrase in self.phrases:
            for stamp in self.stamps:
                if (
                    phrase["time_start"] >= stamp[0]
                    and phrase["time_start"] <= stamp[1]
                ):
                    fragments.append(
                        {"time_sec": phrase["time_start"], "text": phrase["text"]}
                    )
                    continue
        div_text_agg, full_text_agg = self.get_text_state(fragments=fragments)

        self.div_agg = {
            "video": div_vid_agg,
            "audio": div_aud_agg,
            "text": div_text_agg,
        }

        self.full_agg = {
            "video": full_vid_agg,
            "audio": full_aud_agg,
            "text": full_text_agg,
        }

        with open(os.path.join(self.temp_path, "aggression_divided.json"), "w") as file:
            json.dump(self.div_agg, file)

        with open(
            os.path.join(self.temp_path, "aggression_aggregated.json"), "w"
        ) as file:
            json.dump(self.full_agg, file)

        if self.return_path:
            return (
                os.path.join(self.temp_path, "aggression_divided.json"),
                os.path.join(self.temp_path, "aggression_aggregated.json"),
            )
        else:
            return {
                "aggression_divided": self.div_agg,
                "aggression_aggregated": self.full_agg,
            }
