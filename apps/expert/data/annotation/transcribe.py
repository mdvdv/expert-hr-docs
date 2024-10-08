from __future__ import annotations

import base64
import copy
import gzip
import logging
import os
import string
import sys
from typing import List

import numpy as np
import torch
import torch.nn.functional as F
import whisper
from scipy.ndimage import median_filter
from scipy.signal import find_peaks
from whisper.audio import HOP_LENGTH, N_FRAMES, SAMPLE_RATE  # 3000, 160, 16000


class HiddenPrints:
    def __enter__(self):
        self._original_stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")

    def __exit__(self, exc_type, exc_val, exc_tb):
        sys.stdout.close()
        sys.stdout = self._original_stdout


with HiddenPrints():
    import dtw

logger = logging.getLogger("whisper_timestamped")
AUDIO_SAMPLES_PER_TOKEN = HOP_LENGTH * 2  # 320
AUDIO_TIME_PER_TOKEN = AUDIO_SAMPLES_PER_TOKEN / SAMPLE_RATE  # 0.02
USE_EFFICIENT_BY_DEFAULT = True
USE_EFFICIENT_BY_DEFAULT = True
TRUST_WHISPER_TIMESTAMP_BY_DEFAULT = True
DISFLUENCY_MARK = "[*]"


def transcribe_timestamped(
    # Main Whisper options.
    model,
    audio,
    language=None,
    task="transcribe",
    # Additional options for word alignment.
    remove_punctuation_from_words=False,
    compute_word_confidence=True,
    include_punctuation_in_confidence=False,
    refine_whisper_precision=0.5,
    min_word_duration=0.02,
    word_alignement_most_top_layers=None,
    # Reproducibility.
    seed=1234,
    detect_disfluencies=False,
    trust_whisper_timestamps=TRUST_WHISPER_TIMESTAMP_BY_DEFAULT,
    # Other Whisper options.
    temperature=0.0 if USE_EFFICIENT_BY_DEFAULT else (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
    best_of=None,
    patience=None,
    length_penalty=None,
    compression_ratio_threshold=2.4,
    logprob_threshold=-1.0,
    no_speech_threshold=0.6,
    fp16=None,
    condition_on_previous_text=True,
    initial_prompt=None,
    suppress_tokens="-1",
    sample_len=None,
):
    """Transcribe an audio file using Whisper.

    Args:
        model (str): The Whisper model instance.
        audio (str | np.ndarray | torch.Tensor): The path to the audio file to open, or the audio waveform.
        language (str, optional): The language to use for the transcription.
            If None, the language is detected automatically.
        task (str, optional): The task to perform: either "transcribe" or "translate".
        remove_punctuation_from_words (bool, optional): If False, words will be glued
            with the next punctuation mark (if any). If True, there will be no punctuation
            mark in the `words[:]["text"]` list. It only affects these strings; This has
            no influence on the computation of the word confidence, whatever the value
            of `include_punctuation_in_confidence` is.
        compute_word_confidence (bool, optional): Whether to compute word confidence.
            If True, a finer confidence for each segment will be computed as well.
        detect_disfluencies: bool
        Whether to detect disfluencies (i.e. hesitations, filler words, repetitions, corrections, etc.)
        that Whisper model might have omitted in the transcription.
        This should make the word timestamp prediction more accurate.
        And probable disfluencies will be marked as special words "[*]".
        trust_whisper_timestamps: bool
        Whether to rely on Whisper's timestamps to get approximative first
        estimate of segment positions (up to refine_whisper_precision).
        include_punctuation_in_confidence (bool, optional): Whether to include proba of
            punctuation in the computation of the (previous) word confidence.
        refine_whisper_precision (float, optional): How much can we refine Whisper segment
            positions, in seconds. Must be a multiple of 0.02.
        min_word_duration (float, optional): Minimum duration of a word, in seconds.
            If a word is shorter than this, timestamps will be adjusted.
        seed (int, optional): Random seed to use for temperature sampling,
            for the sake of reproducibility. Choose None for unpredictable randomness.
        temperature (float, optional): Temperature for sampling.
        compression_ratio_threshold (float, optional): If the gzip compression ratio
            is above this value, treat as failed.
        logprob_threshold (float, optional): If the average log probability over sampled
            tokens is below this value, treat as failed.
        no_speech_threshold (float, optional): If the no_speech probability is higher than this value
            AND the average log probability over sampled tokens is below `logprob_threshold`,
            consider the segment as silent.
        condition_on_previous_text (bool, optional): If True, the previous output of the model is
            provided as a prompt for the next window; disabling may make the text inconsistent
            across windows, but the model becomes less prone to getting stuck in a failure loop,
            such as repetition looping or timestamps going out of sync.
        initial_prompt (str, optional): Optional text to provide as a prompt for the first window.
        suppress_tokens (str, optional): Comma-separated list of token ids to suppress during sampling;
            '-1' will suppress most special characters except common punctuations.

    Returns:
        Dict: A dictionary containing the resulting text ("text") and segment-level details ("segments"), and
            the spoken language ("language"), which is detected when `decode_options["language"]` is None.
    """

    if seed is not None:
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)

    # Check input options.naive_approach
    assert (
        refine_whisper_precision >= 0
        and refine_whisper_precision / AUDIO_TIME_PER_TOKEN
        == round(refine_whisper_precision / AUDIO_TIME_PER_TOKEN)
    ), f"refine_whisper_precision must be a positive multiple of {AUDIO_TIME_PER_TOKEN}"
    refine_whisper_precision_nframes = round(
        refine_whisper_precision / AUDIO_TIME_PER_TOKEN
    )
    assert min_word_duration >= 0, "min_word_duration must be a positive number"
    assert (
        word_alignement_most_top_layers is None or word_alignement_most_top_layers > 0
    ), "word_alignement_most_top_layers must be a strictly positive number"

    if isinstance(temperature, (list, tuple)) and len(temperature) == 1:
        temperature = temperature[0]

    # Input options.
    if fp16 is None:
        fp16 = model.device != torch.device("cpu")

    # Safety check.
    input_stride = N_FRAMES // model.dims.n_audio_ctx
    time_precision = input_stride * HOP_LENGTH / SAMPLE_RATE
    assert time_precision == AUDIO_TIME_PER_TOKEN

    alignment_options = dict(
        remove_punctuation_from_words=remove_punctuation_from_words,
        compute_word_confidence=compute_word_confidence,
        include_punctuation_in_confidence=include_punctuation_in_confidence,
        detect_disfluencies=detect_disfluencies,
        refine_whisper_precision_nframes=refine_whisper_precision_nframes,
        word_alignement_most_top_layers=word_alignement_most_top_layers,
        alignment_heads=(
            get_alignment_heads(model)
            if word_alignement_most_top_layers is None
            else None
        ),
    )
    whisper_options = dict(
        language=language,
        task=task,
        fp16=fp16,
        temperature=temperature,
        best_of=best_of,
        patience=patience,
        length_penalty=length_penalty,
        condition_on_previous_text=condition_on_previous_text,
        initial_prompt=initial_prompt,
        suppress_tokens=suppress_tokens,
        sample_len=sample_len,
    )
    other_options = dict(
        no_speech_threshold=no_speech_threshold,
        logprob_threshold=logprob_threshold,
        compression_ratio_threshold=compression_ratio_threshold,
    )
    (transcription, words) = _transcribe_timestamped_efficient(
        model,
        audio,
        trust_whisper_timestamps=trust_whisper_timestamps,
        **alignment_options,
        **whisper_options,
        **other_options,
    )

    transcription, words = remove_last_null_duration_words(
        transcription, words, recompute_text=True
    )
    # Refine word positions.
    ensure_increasing_positions(words, min_duration=min_word_duration)

    whisper_segments = transcription["segments"]
    for word in words:
        word.pop("tokens")
        word.pop("tokens_indices")
        if "avg_logprob_reliable" in word:
            word.pop("avg_logprob_reliable")
        idx_segment = word.pop("idx_segment")
        segment = whisper_segments[idx_segment]
        if "words" in segment:
            segment["words"].append(word)
        else:
            segment["words"] = [word]
            if refine_whisper_precision:
                segment["start"] = word["start"]
        if refine_whisper_precision:
            segment["end"] = word["end"]

    return transcription


def _transcribe_timestamped_efficient(
    model,
    audio,
    remove_punctuation_from_words,
    compute_word_confidence,
    include_punctuation_in_confidence,
    refine_whisper_precision_nframes,
    alignment_heads,
    word_alignement_most_top_layers,
    detect_disfluencies,
    trust_whisper_timestamps,
    use_timestamps_for_alignment=True,
    # Whisper specific options.
    **whisper_options,
):
    # Get options.
    sample_len = whisper_options["sample_len"]
    temperature = whisper_options["temperature"]
    no_speech_threshold = whisper_options["no_speech_threshold"]
    logprob_threshold = whisper_options["logprob_threshold"]

    logit_filters = get_logit_filters(model, whisper_options)
    language = whisper_options["language"]
    tokenizer = whisper.tokenizer.get_tokenizer(
        model.is_multilingual, task=whisper_options["task"], language=language
    )

    max_sample_len = sample_len or model.dims.n_text_ctx // 2
    n_ctx = model.dims.n_text_ctx

    word_alignement_most_top_layers = (
        float("inf")
        if word_alignement_most_top_layers is None
        else word_alignement_most_top_layers
    )

    timestamped_word_segments = []
    segment_tokens = [[]]
    segment_attweights = [
        []
        for _ in range(min(word_alignement_most_top_layers, len(model.decoder.blocks)))
    ]

    segment_avglogprobs = []
    segment_logprobs = []  # Token log probabilities for each segment.
    # Variables related to options that can skip some segments.
    # Index of the SOT token in the current set of processed tokens.
    sot_index = None
    # No speech probability for the current 30 sec chunk.
    no_speech_prob = None
    # Log probabilities for the current 30 sec chunk.
    chunk_logprobs = []
    # Tokens for the current 30 sec chunk (list of Torch tensors).
    chunk_tokens = []
    # Tokens for the current 30 sec chunk, without the SOT tokens (list of indices).
    chunk_tokens_nosot = []
    # Last token to use as a fallback if the model gets stuck.
    last_token_fallback = None
    last_chunk_token = None
    has_started = False  # Whether we have started decoding.
    # MFCC features for the current 30 sec chunk.
    mfcc = None
    new_mfcc = None

    def is_sot(curr_tokens):
        return (
            curr_tokens is None
            or len(curr_tokens) > 1
            or curr_tokens[0] == tokenizer.sot
        )

    def has_reached_decoding_limit():
        n = len(chunk_tokens_nosot) + 1
        m = n + (len(chunk_tokens[0]) if len(chunk_tokens) > 0 else 0)
        return n >= max_sample_len or m > n_ctx

    def reset(add_segment, keep_last_token=True):
        """Reset the list of tokens for the current speech segment, and corresponding cross-attention weights"""
        nonlocal segment_tokens, segment_attweights
        if add_segment:
            if keep_last_token:
                segment_tokens.append([segment_tokens[-1][-1]])
                segment_attweights = [w[-1:] for w in segment_attweights]
            else:
                segment_tokens.append([])
                segment_attweights = [[] for w in segment_attweights]
            segment_tokens[-2].pop(0)
        elif len(segment_tokens[-1]) > 0:
            segment_tokens[-1] = []
            segment_attweights = [[] for w in segment_attweights]

    saw_consecutive_timestamps = False

    def must_flush_segment(curr_tokens):
        """Return whether or not the previously collected tokens must be used to add a new speech segment"""

        nonlocal segment_tokens, saw_consecutive_timestamps, chunk_tokens_nosot
        if not is_sot(curr_tokens):
            is_timestamp = curr_tokens[0] >= tokenizer.timestamp_begin
            is_previous_timestamp = (
                segment_tokens[-1][-1] >= tokenizer.timestamp_begin
                if len(segment_tokens[-1]) > 0
                else False
            )
            consecutive_timestamps = is_timestamp and is_previous_timestamp
            if consecutive_timestamps:
                saw_consecutive_timestamps = True
            if len(chunk_tokens_nosot) == max_sample_len - 2 and is_timestamp:
                consecutive_timestamps = True
            return consecutive_timestamps
        else:  # Several tokens as a prompt or must flush last segments
            must_flush = len(segment_tokens[-1]) > 1 and not saw_consecutive_timestamps
            if (
                not must_flush
            ):  # If the last token is a timestamp, the last segment is used
                if last_chunk_token is None:
                    must_flush = (
                        len(segment_tokens[-1]) > 2
                        and segment_tokens[-1][-1] >= tokenizer.timestamp_begin
                    )
                else:
                    must_flush = last_chunk_token >= tokenizer.timestamp_begin
            if not must_flush and trust_whisper_timestamps:
                # Discard the end of the last transcription
                reset(False)
            saw_consecutive_timestamps = False
            return must_flush

    index_begin_30sec_chunck = 0

    def get_index_begin_30sec_chunck(curr_tokens):
        nonlocal index_begin_30sec_chunck

        if is_sot(curr_tokens) and has_started:
            if trust_whisper_timestamps:
                res = index_begin_30sec_chunck
                index_begin_30sec_chunck = len(segment_tokens) - 1
            else:
                res = len(segment_tokens) - 1
            return res

    def align_last_segment(curr_tokens=None):
        nonlocal segment_tokens, segment_attweights, timestamped_word_segments
        nonlocal has_started, no_speech_prob, chunk_tokens, chunk_tokens_nosot
        nonlocal chunk_logprobs, mfcc, new_mfcc, logit_filters
        nonlocal index_begin_30sec_chunck, last_token_fallback

        tokens = segment_tokens[-1][1:]

        # When the decoding hit the max limit (number of tokens) -- usually when the language model gets stuck --
        # then we have to recover the last token from what is send to the decoder
        unfinished_decoding = len(tokens) and tokens[-1] < tokenizer.timestamp_begin
        last_token_reliable = True

        if unfinished_decoding:
            logger.debug(
                f"WARNING: decoding hit the max limit for segment {segment_tokens} (It usually happens when the language model gets stuck)"
            )
            # The last token chosen is in the prompt for the new chunk
            if curr_tokens is not None and curr_tokens[0] == tokenizer.sot_prev:
                index_sot = (curr_tokens == tokenizer.sot).nonzero(as_tuple=True)
                assert len(index_sot) == 1
                index_sot = index_sot[0].item()
                assert index_sot > 0
                last_token_fallback = curr_tokens[index_sot - 1].item()
                logger.debug(
                    f"Guessed last token from the prompt for the new chunk: {last_token_fallback}"
                )
            # Fallback for the last segment, or without prompt: Assume greedy decoding
            else:
                last_token_fallback = (
                    torch.argmax(chunk_logprobs[-1]).item()
                    if last_chunk_token is None
                    else last_chunk_token
                )
                last_token_reliable = temperature == 0
                logger.debug(
                    f"Guess last token using probas (assuming greedy decoding): {last_token_fallback}"
                )

            tokens.append(last_token_fallback)
            segment_tokens[-1].append(last_token_fallback)
            attention_weights = [torch.cat(w, dim=-2) for w in segment_attweights]
            last_logprobs = chunk_logprobs[-1]
        else:
            attention_weights = [torch.cat(w[:-1], dim=-2) for w in segment_attweights]
            last_logprobs = chunk_logprobs[-2]

        # Check prediction of last token
        end_token = tokens[-1]
        if end_token >= tokenizer.timestamp_begin:
            start_token = tokens[0]
            assert start_token >= tokenizer.timestamp_begin
            # If Whisper prediction of the end is obviously wrong, we predict it again (constrained)
            if end_token <= start_token:
                new_end_token = (
                    last_logprobs[start_token + 1 :].argmax() + start_token + 1
                )
                tokens[-1] = new_end_token.item()

        if len(tokens) <= 1:
            # Corner case: nothing in between timestamps
            ws = []
        else:
            ws = perform_word_alignment(
                tokens,
                attention_weights,
                tokenizer,
                use_space=should_use_space(language),
                alignment_heads=alignment_heads,
                remove_punctuation_from_words=remove_punctuation_from_words,
                refine_whisper_precision_nframes=refine_whisper_precision_nframes,
                detect_disfluencies=detect_disfluencies,
                unfinished_decoding=unfinished_decoding,
                mfcc=mfcc,
            )

        add_segment = len(ws) > 0
        if add_segment:
            timestamped_word_segments.append(ws)
        else:
            logger.debug(f"Not added!")
        reset(add_segment, not is_sot(curr_tokens))

        return add_segment, unfinished_decoding, last_token_reliable

    def may_flush_segment(curr_tokens=None):
        """Add a speech segment with the new tokens if necessary.
        May also remove the last collected segments if filtered out by Whisper (no_speech_prob <= no_speech_threshold)
        """
        nonlocal segment_tokens, segment_attweights, timestamped_word_segments
        nonlocal segment_logprobs, has_started, no_speech_prob, logit_filters
        nonlocal chunk_tokens, chunk_tokens_nosot, chunk_logprobs, mfcc, new_mfcc
        nonlocal last_token_fallback, last_chunk_token, index_begin_30sec_chunck

        # Check if a new segment should be added
        unfinished_decoding = False
        last_token_reliable = True

        if must_flush_segment(curr_tokens) and trust_whisper_timestamps:
            _, unfinished_decoding, last_token_reliable = align_last_segment(
                curr_tokens
            )

        i_start = get_index_begin_30sec_chunck(curr_tokens)

        # All segments from previous 30sec chunck have been collected
        if i_start is not None:
            if not trust_whisper_timestamps:
                tokens = torch.Tensor(segment_tokens[-1]).int()
                idx_task = torch.where(tokens == tokenizer.sot_sequence[-1])[0][
                    0
                ].item()  # index of <|transcribe|>

                is_special = tokens.ge(tokenizer.eot)
                # Remove prompt
                is_special[:idx_task] = True
                # Keep begin timestamp
                is_special[idx_task : idx_task + 2] = False

                is_timestamp = tokens.ge(tokenizer.timestamp_begin)
                consecutive = torch.where(is_timestamp[1:] & is_timestamp[:-1])[0]
                if (has_reached_decoding_limit()) and (
                    (is_timestamp[-1] and not is_timestamp[-2])
                    if last_chunk_token is None
                    else last_chunk_token >= tokenizer.timestamp_begin
                    and not is_timestamp[-2]
                ):
                    consecutive = torch.cat(
                        [consecutive, torch.Tensor([len(tokens) - 1]).int()]
                    )
                last_is_timestamp = True
                if len(consecutive):
                    # Remove last tokens
                    is_special[consecutive[-1] + 1 :] = True
                    # Keep end timestamp
                    is_special[consecutive[-1]] = False
                elif is_timestamp[-1]:
                    # Keep end timestamp
                    is_special[-1] = False
                else:
                    last_is_timestamp = False

                if use_timestamps_for_alignment and len(consecutive):
                    # Keep all timestamps
                    is_special[idx_task + 2 : consecutive[-1]] = False

                # Do remove what has to be removed
                is_next_achar = ~torch.cat(
                    [is_special[1:], torch.Tensor([False]).bool()]
                )
                for i, weights in enumerate(segment_attweights):
                    assert len(weights) == len(
                        tokens
                    ), f"{len(weights)} attention weights != {len(tokens)}"
                    # We must remove attention weights used to predict timestamp tokens
                    segment_attweights[i] = [
                        w for s, w in zip(is_next_achar, weights) if s
                    ]
                tokens_filtered = tokens[~is_special]
                assert len(segment_attweights[0]) == len(
                    tokens_filtered
                ), f"{len(segment_attweights[0])} attention weights != {len(tokens_filtered)} "

                # Replace first and last timestamp
                orig_start, orig_end = (
                    tokens_filtered[1].item(),
                    tokens_filtered[-1].item(),
                )
                tokens_filtered[1] = tokenizer.timestamp_begin  # <|0.00|>
                if last_is_timestamp:
                    tokens_filtered[-1] = (
                        tokenizer.timestamp_begin + N_FRAMES // 2
                    )  # <|30.00|>
                segment_tokens[-1] = tokens_filtered.tolist()

                # Do alignement
                (
                    added,
                    unfinished_decoding,
                    last_token_reliable,
                ) = align_last_segment()

                # Re-split into segments (if necessary)
                if added:
                    if len(consecutive) > 1:
                        segments_timestamped_concat = timestamped_word_segments[-1]
                        new_segments_timestamped = []
                        new_segment_tokens = []
                        start = idx_task + 1
                        i_word = 0
                        for i, end in enumerate(consecutive):
                            end = end.item()
                            new_segment_tokens.append(tokens[start : end + 1].tolist())
                            total_length = end - start - 1
                            start = end + 1
                            length = 0
                            new_segments_timestamped.append([])
                            while length < total_length:
                                if not use_timestamps_for_alignment and i_word == len(
                                    segments_timestamped_concat
                                ):
                                    # This can happen in the case of "..."
                                    assert (
                                        total_length == 1 and i == len(consecutive) - 1
                                    ), "Unexpected situation!"
                                    break
                                assert i_word < len(
                                    segments_timestamped_concat
                                ), f"i_word={i_word} < len(segments_timestamped_concat)={len(segments_timestamped_concat)}"
                                word = segments_timestamped_concat[i_word]
                                new_segments_timestamped[-1].append(word)
                                length += len(word["tokens_indices"])
                                i_word += 1
                            # This can be non zero, when a punctuation (alone in a segment) is glued to the previous segment
                            if use_timestamps_for_alignment:
                                assert (
                                    length == total_length
                                ), f"length={length} != total_length={total_length}"
                            elif length > total_length:
                                delta = length - total_length
                                word = new_segments_timestamped[-1][-1]
                                word_tokindices = word["tokens_indices"]
                                word_tokens = word["tokens"]
                                word["tokens_indices"] = word_tokindices[:-delta]
                                word["tokens"] = word_tokens[:-delta]
                                word["word"] = "".join(word_tokens[:-delta])
                                i_word -= 1
                                t = segments_timestamped_concat[i_word]["end"]
                                segments_timestamped_concat[i_word] = dict(
                                    text="".join(word_tokens[-delta:]),
                                    start=t,
                                    end=t,  # Word without timestamp
                                    tokens=word_tokens[-delta:],
                                    tokens_indices=word_tokindices[-delta:],
                                )

                        assert i_word == len(segments_timestamped_concat)

                        segment_tokens = (
                            segment_tokens[:-2]
                            + new_segment_tokens
                            + [segment_tokens[-1]]
                        )
                        timestamped_word_segments = (
                            timestamped_word_segments[:-1] + new_segments_timestamped
                        )
                    else:
                        # Recover start and end token
                        segment = segment_tokens[-2]
                        tokenizer.decode_with_timestamps([orig_start, orig_end])
                        segment[0] = orig_start
                        if last_is_timestamp:
                            segment[-1] = orig_end

                    if unfinished_decoding:
                        timestamped_word_segments[-1][-1][
                            "avg_logprob_reliable"
                        ] = last_token_reliable

                reset(False)

            mfcc = new_mfcc
            n_segments = len(segment_tokens) - 1

            # Get word confidence and/or check if previous segments shoud have been skipped
            should_skip = False
            if compute_word_confidence or no_speech_threshold is not None:
                # no voice activity check
                should_skip = (
                    (no_speech_prob > no_speech_threshold)
                    if (no_speech_threshold is not None)
                    else False
                )
                if compute_word_confidence or (
                    should_skip and logprob_threshold is not None
                ):
                    n = len(chunk_logprobs)
                    if n == len(chunk_tokens_nosot):
                        chunk_tokens_nosot = chunk_tokens_nosot[1:]
                    if unfinished_decoding:
                        assert last_token_fallback is not None
                        last_tokens = [last_token_fallback]
                        timestamped_word_segments[-1][-1][
                            "avg_logprob_reliable"
                        ] = last_token_reliable
                        n += 1
                    elif has_reached_decoding_limit():
                        # there were segments in the 30sec chunck, and then the LM got stuck
                        last_tokens = [torch.argmax(chunk_logprobs[-1]).item()]
                        timestamped_word_segments[-1][-1]["avg_logprob_reliable"] = (
                            temperature == 0
                        )
                    else:
                        last_tokens = [tokenizer.eot]
                    chunck_indices = chunk_tokens_nosot + last_tokens
                    assert len(chunk_logprobs) == len(
                        chunck_indices
                    ), f"{len(chunk_logprobs)} != {len(chunck_indices)}"
                    logprobs = torch.cat(
                        [
                            logprob[i].unsqueeze(0)
                            for (logprob, i) in zip(chunk_logprobs, chunck_indices)
                        ]
                    )
                    assert min(
                        [p.isfinite().item() for p in logprobs]
                    ), f"Got infinite logprob among ({len(logprobs)}) {[(i, tokenizer.decode_with_timestamps([i]), v.item()) for (i,v) in zip(chunck_indices, logprobs)]}"
                    sum_logprob = sum(logprobs)
                    avg_logprob = sum_logprob / n
                    # don't skip if the logprob is high enough, whatever the no_speech_prob is
                    if (
                        logprob_threshold is not None
                        and avg_logprob > logprob_threshold
                    ):
                        should_skip = False

                if should_skip:
                    logger.debug(
                        f"Skipping last {n_segments-i_start} segments (no_speech_prob {no_speech_prob} > {no_speech_threshold} and avg_logprob {avg_logprob} < {logprob_threshold})"
                    )
                    index_begin_30sec_chunck -= n_segments - i_start
                    segment_tokens = segment_tokens[:i_start] + [segment_tokens[-1]]
                    timestamped_word_segments = timestamped_word_segments[:i_start]
                elif compute_word_confidence:
                    avg_logprob = avg_logprob.item()
                    i_token_end = -1
                    for i in range(i_start, n_segments):
                        tokens = segment_tokens[i]
                        i_token_start = i_token_end + 1
                        i_token_end = i_token_start + len(tokens)
                        assert (
                            chunck_indices[i_token_start:i_token_end] == tokens
                        ), f"Inconsistent token list {tokenizer.decode_with_timestamps(chunck_indices[i_token_start:i_token_end])} != {tokenizer.decode_with_timestamps(tokens)}"
                        i_token_start += 1  # skip sos (start time)
                        if not unfinished_decoding or i != n_segments - 1:
                            i_token_end -= 1  # skip eos (end time)
                        segment_logprobs.append(logprobs[i_token_start:i_token_end])
                        segment_avglogprobs.append(avg_logprob)
                else:
                    for i in range(i_start, n_segments):
                        segment_logprobs.append(None)
                        segment_avglogprobs.append(None)
            else:
                for i in range(i_start, n_segments):
                    segment_logprobs.append(None)
                    segment_avglogprobs.append(None)

            # Reset counters
            chunk_tokens = []
            chunk_tokens_nosot = []
            chunk_logprobs = []
            no_speech_prob = None

    def hook_attention_weights(layer, ins, outs, index):
        nonlocal segment_attweights
        if not has_started:
            return
        w = outs[-1]
        # Only the last attention weights is useful
        if w.shape[-2] > 1:
            w = w[:, :, -1:, :]
        segment_attweights[index].append(w.cpu())

    def hook_attention_weights(layer, ins, outs, index):
        nonlocal segment_attweights
        if not has_started:
            return
        w = outs[-1]
        # Only the last attention weights is useful
        if w.shape[-2] > 1:
            w = w[:, :, -1:, :]
        segment_attweights[index].append(w.cpu())

    def hook_mfcc(layer, ins, outs):
        nonlocal new_mfcc, mfcc
        new_mfcc = ins[0]
        if mfcc is None:
            mfcc = new_mfcc

    def hook_input_tokens(layer, ins, outs):
        nonlocal segment_tokens, sot_index, chunk_tokens, chunk_tokens_nosot
        nonlocal logit_filters, has_started, language

        curr_tokens = ins[0]
        assert curr_tokens.shape[0] == 1, "Batch decoding is not supported"
        curr_tokens = curr_tokens.squeeze(0)

        if is_sot(curr_tokens):
            chunk_prompt = curr_tokens.tolist()
            if language is None:
                if len(curr_tokens) > 1:
                    language = tokenizer.decode(curr_tokens[-2:-1])
                    language = language[2:-2]  # remove trailing "<|" and "|>"
                    whisper_options["language"] = language

            logit_filters = get_logit_filters(
                model,
                whisper_options,
                prompt=chunk_prompt[1 : -len(tokenizer.sot_sequence)],
            )

        may_flush_segment(curr_tokens)

        # Get the index of the <|startoftranscript|> tokens (to get proba of silence later)
        if is_sot(curr_tokens):
            has_started = len(curr_tokens) > 1 or not model.is_multilingual
            if no_speech_threshold is not None:
                sot_index = curr_tokens.tolist().index(tokenizer.sot)
        else:
            sot_index = None

        # Keep the last token only
        if has_started:
            segment_tokens[-1].append(curr_tokens[-1].item())

        # Accumulate tokens
        if has_started:
            chunk_tokens.append(curr_tokens)
            if not is_sot(curr_tokens):
                chunk_tokens_nosot.append(curr_tokens[-1].item())

    embedding_weights = None

    def hook_output_logits(layer, ins, outs):
        nonlocal no_speech_prob, chunk_logprobs, segment_tokens, chunk_tokens, embedding_weights, has_started

        if embedding_weights is None:
            embedding_weights = torch.transpose(
                model.decoder.token_embedding.weight, 0, 1
            ).to(outs[0].dtype)

        # Get the probability of silence.
        if sot_index is not None:
            logits = (outs[0][sot_index, :] @ embedding_weights).float()
            logits = logits.softmax(dim=-1)
            no_speech_prob = logits[tokenizer.no_speech].item()

        # Get the log-probabilities of tokens (we don't know yet which one will be chosen).
        if has_started:
            logits = (outs[0][-1:, :] @ embedding_weights).float()
            tokens = torch.cat(chunk_tokens).unsqueeze(0)
            for logit_filter in logit_filters:
                logit_filter.apply(logits, tokens)
            logits = F.log_softmax(logits.squeeze(0), dim=-1)
            chunk_logprobs.append(logits)

    try:
        # Add hooks to the model, to get tokens and attention weights on the fly.
        all_hooks = []
        all_hooks.append(model.encoder.conv1.register_forward_hook(hook_mfcc))
        all_hooks.append(
            model.decoder.token_embedding.register_forward_hook(hook_input_tokens)
        )
        nblocks = len(model.decoder.blocks)
        j = 0
        for i, block in enumerate(model.decoder.blocks):
            if i < nblocks - word_alignement_most_top_layers:
                continue
            all_hooks.append(
                block.cross_attn.register_forward_hook(
                    lambda layer, ins, outs, index=j: hook_attention_weights(
                        layer, ins, outs, index
                    )
                )
            )
            j += 1
        if compute_word_confidence or no_speech_threshold is not None:
            all_hooks.append(model.decoder.ln.register_forward_hook(hook_output_logits))

        transcription = model.transcribe(audio, **whisper_options)

    finally:
        # Remove hooks.
        for hook in all_hooks:
            hook.remove()

    # Finalize (collect last segment).
    may_flush_segment()
    segment_tokens.pop(-1)

    token_special_idx = min(tokenizer.sot, tokenizer.eot)

    def filter_tokens(tokens):
        while len(tokens) and tokens[0] >= token_special_idx:
            tokens = tokens[1:]
        while len(tokens) and tokens[-1] >= token_special_idx:
            tokens = tokens[:-1]
        return tokens

    assert len(segment_tokens) == len(
        timestamped_word_segments
    ), f"Inconsistent number of segments: tokens ({len(segment_tokens)}) != timestamped_word_segments ({len(timestamped_word_segments)})"
    assert len(segment_avglogprobs) == len(
        segment_tokens
    ), f"Inconsistent number of segments: avg logprobs ({len(segment_avglogprobs)}) != tokens ({len(segment_tokens)})"
    assert len(segment_logprobs) == len(
        segment_tokens
    ), f"Inconsistent number of segments: logprobs ({len(segment_logprobs)}) != tokens ({len(segment_tokens)})"

    whisper_segments = transcription["segments"]
    l1 = len(whisper_segments)
    l2 = len(timestamped_word_segments)
    if l1 != l2 and l1 != 0:
        logger.warning(
            f"Inconsistent number of segments: whisper_segments ({l1}) != timestamped_word_segments ({l2})"
        )
    assert (
        l1 == l2 or l1 == 0
    ), f"Inconsistent number of segments: whisper_segments ({l1}) != timestamped_word_segments ({l2})"

    logger.debug("Compile results")
    words = []
    for i, (
        segment,
        timestamped_words,
        token,
        avglogprob,
        logprobs,
    ) in enumerate(
        zip(
            whisper_segments,
            timestamped_word_segments,
            segment_tokens,
            segment_avglogprobs,
            segment_logprobs,
        )
    ):
        timestamped_tokens = filter_tokens(token)
        whisper_tokens = filter_tokens(segment["tokens"])
        if timestamped_tokens != whisper_tokens:
            if len(timestamped_tokens) == len(whisper_tokens) + 1:
                logger.warn(f"An additional token was added on segment {i}")
            else:
                assert (
                    len(timestamped_tokens) < len(whisper_tokens)
                    and timestamped_tokens == whisper_tokens[: len(timestamped_tokens)]
                ), f"""Fatal Error: Got inconsistent text for segment {i}:
                    \n({len(timestamped_tokens)})\n{tokenizer.decode_with_timestamps(timestamped_tokens)}
                    \n{timestamped_tokens}\n!=\n({len(whisper_tokens)})\
                    n{tokenizer.decode_with_timestamps(whisper_tokens)}\n{whisper_tokens[:len(timestamped_tokens)]}"""
                logger.warn(
                    f"Text had to be shortned on segment {i}:\n{tokenizer.decode(timestamped_tokens)}\n!=\n{tokenizer.decode(whisper_tokens)}"
                )
            timestamped_words[-1]["avg_logprob_reliable"] = False

        offset = segment["seek"] * HOP_LENGTH / SAMPLE_RATE
        for timestamped_word in timestamped_words:
            timestamped_word["start"] += offset
            timestamped_word["end"] += offset
            timestamped_word["idx_segment"] = i

        if compute_word_confidence:
            if (
                "avg_logprob_reliable" not in timestamped_words[-1]
                or timestamped_words[-1]["avg_logprob_reliable"]
            ):
                if abs(segment["avg_logprob"] - avglogprob) >= 1e-2:
                    logger.warn(
                        f"Recomputed different logprob for segment {i}: {avglogprob} != {segment['avg_logprob']}"
                    )
            if include_punctuation_in_confidence:
                segment["confidence"] = round(logprobs.mean().exp().item(), 3)
            else:
                logprobs_nopunc = []
            i_end = 0
            for timestamped_word in timestamped_words:
                i_start = i_end
                tokens = timestamped_word["tokens"]
                i_end += len(tokens)

                assert i_end <= len(
                    logprobs
                ), f"Fatal Error: Got out-of-bound index for segment {i}: {i_end} > {len(logprobs)}"
                if include_punctuation_in_confidence:
                    word_logprobs = logprobs[i_start:i_end]
                else:
                    # Note: look at the last character of token, to take into account "...", "!!", etc.
                    while len(tokens) > 1 and tokens[-1][-1] in _punctuation:
                        tokens = tokens[:-1]
                    word_logprobs = logprobs[i_start : i_start + len(tokens)]
                    logprobs_nopunc.append(word_logprobs)

                timestamped_word["confidence"] = round(
                    word_logprobs.mean().exp().item(), 3
                )

            if i_end != len(logprobs):
                logger.warn(
                    f"Got inconsistent length for segment {i} ({len(logprobs)} != {i_end}). Some words have been ignored."
                )
            if not include_punctuation_in_confidence:
                logprobs_nopunc = torch.cat(logprobs_nopunc)
                segment["confidence"] = round(logprobs_nopunc.mean().exp().item(), 3)

        words.extend(timestamped_words)

    return transcription, words


def audio_minimum_padding(audio):
    if audio.shape[-1] <= 200:
        return whisper.pad_or_trim(audio, 201)
    return audio


def should_use_space(language):
    return norm_language(language) not in ["zh", "ja", "th", "lo", "my"]


def norm_language(language):
    if language is None:
        return "en"
    return whisper.tokenizer.TO_LANGUAGE_CODE.get(language.lower(), language)


def get_logit_filters(model, whisper_options, prompt=None):
    decoding_options = get_decoding_options(whisper_options)
    if "initial_prompt" in decoding_options:
        prompt0 = decoding_options.pop("initial_prompt")
        if prompt is None:
            prompt = prompt0
    if prompt is not None:
        decoding_options["prompt"] = prompt
    decoding_options = whisper.DecodingOptions(
        without_timestamps=False,
        max_initial_timestamp=1.0,
        prefix=None,
        suppress_blank=True,
        **decoding_options,
    )

    # This performs some checks on the options.
    decoding_task = whisper.decoding.DecodingTask(model, decoding_options)
    return decoding_task.logit_filters


def get_decoding_options(whisper_options):
    return dict(
        [
            (k, v)
            for (k, v) in whisper_options.items()
            if k
            not in [
                "no_speech_threshold",
                "logprob_threshold",
                "compression_ratio_threshold",
                "condition_on_previous_text",
            ]
        ]
    )


def perform_word_alignment(
    tokens,
    attention_weights,
    tokenizer,
    use_space=True,
    mfcc=None,
    refine_whisper_precision_nframes=0,
    remove_punctuation_from_words=False,
    include_punctuation_in_timing=False,  # Was True before 1.9
    unfinished_decoding=False,
    alignment_heads=None,
    medfilt_width=9,
    qk_scale=1.0,
    detect_disfluencies=True,
    subwords_can_be_empty=True,  # Was False before 1.11
):
    """
    Perform word alignment on the given tokens and attention weights.
    Returns a list of (word, start_time, end_time) tuples.

    tokens: list of tokens (integers)
    attention_weights: list of attention weights (torch tensors)
    tokenizer: tokenizer used to tokenize the text
    use_space: whether to use spaces to split the tokens into words (should be true for all languages except Japanese, Chinese, ...)
    mfcc: MFCC features (used to identify padded region)
    refine_whisper_precision_nframes: precision time
    remove_punctuation_from_words: whether to remove punctuation from words
    include_punctuation_in_timing: whether to include punctuation in the timing of (previous) words
    unfinished_decoding: whether the decoding is unfinished (e.g. because the model is stuck)
    alignment_heads: list of attention heads to use for alignment
    medfilt_width: width of the median filter used to smooth the attention weights
    qk_scale: scale factor applied to the attention weights
    """

    assert (
        len(tokens) > 1
    ), f"Got unexpected sequence of tokens of length {len(tokens)} {tokenizer.decode_with_timestamps(tokens)}"
    start_token = tokens[0] - tokenizer.timestamp_begin
    end_token = tokens[-1] - tokenizer.timestamp_begin

    # Check start / end tokens
    if start_token < 0:
        raise RuntimeError(
            f"Missing start token in: {tokenizer.decode_with_timestamps(tokens)}"
        )
    if len(tokens) == 1 or end_token < 0:
        # This can happens when Whisper is stucked as a Language Model
        end_token = N_FRAMES // 2
    if end_token == start_token and refine_whisper_precision_nframes == 0:
        return []

    # Put some margin around the segment
    if refine_whisper_precision_nframes > 0:
        start_token = max(start_token - refine_whisper_precision_nframes, 0)
        end_token = min(end_token + refine_whisper_precision_nframes, N_FRAMES // 2)

    if end_token <= start_token:
        raise RuntimeError(
            f"Got segment with null or negative duration {tokenizer.decode_with_timestamps(tokens)}: {start_token} {end_token}"
        )

    start_time = start_token * AUDIO_TIME_PER_TOKEN
    end_time = end_token * AUDIO_TIME_PER_TOKEN

    split_tokens = split_tokens_on_spaces if use_space else split_tokens_on_unicode
    words, word_tokens, word_tokens_indices = split_tokens(
        tokens,
        tokenizer,
        remove_punctuation_from_words=remove_punctuation_from_words,
    )

    # If the last token is a punctuation that comes after a word
    # group this final punctuation with the final timestamp
    # This is to avoid assigning the final punctuation to a big silence or a noise/music background coming after
    num_punctuations_per_tokens = [
        0 if len(w) == 1 or w[-1] not in _punctuation else 1 for w in word_tokens
    ]
    if include_punctuation_in_timing:
        num_punctuations_per_tokens[:-2] = [0] * (len(num_punctuations_per_tokens) - 2)

    for i, w in enumerate(attention_weights):
        assert w.shape[-2] == len(
            tokens
        ), f"Attention weights have wrong shape: {w.shape[-2]} (expected {len(tokens)})."
    weights = torch.cat(attention_weights)  # layers * heads * tokens * frames

    num_tokens = weights.shape[-2]
    num_frames = end_token - start_token
    if num_tokens > num_frames:
        logger.warning(
            f"Too much text ({num_tokens} tokens) for the given number of frames ({num_frames}) in: {tokenizer.decode_with_timestamps(tokens)}\nThe end of the text will be removed."
        )
        return perform_word_alignment(
            tokens[: num_frames - 1] + [tokens[-1]],
            [
                torch.cat([w[:, :, : num_frames - 1, :], w[:, :, -1:, :]], dim=-2)
                for w in attention_weights
            ],
            tokenizer,
            use_space=use_space,
            refine_whisper_precision_nframes=refine_whisper_precision_nframes,
            medfilt_width=medfilt_width,
            qk_scale=qk_scale,
            alignment_heads=alignment_heads,
            mfcc=mfcc,
            remove_punctuation_from_words=remove_punctuation_from_words,
            detect_disfluencies=detect_disfluencies,
            subwords_can_be_empty=subwords_can_be_empty,
            unfinished_decoding=True,
        )

    assert end_token <= weights.shape[-1]
    assert len(tokens) == num_tokens

    weights = weights[
        ..., start_token:end_token
    ].cpu()  # layers * heads * tokens * frames

    if alignment_heads is None:
        weights = weights.reshape(-1, *weights.shape[-2:])  # N * tokens * frames
    else:
        weights = torch.stack([weights[l][h] for l, h in alignment_heads.indices().T])
    weights = median_filter(weights, (1, 1, medfilt_width))
    weights = torch.tensor(weights * qk_scale).softmax(dim=-1)
    weights = weights.mean(axis=(0))  # average over layers and heads. tokens * frames
    weights = weights / weights.norm(
        dim=-2, keepdim=True
    )  # This was before the mean before 1.9
    weights = -weights.double().numpy()
    worse_weight = 0

    # Get the limit of audio duration
    max_duration = None
    if mfcc is not None:
        max_duration = find_start_padding(mfcc)
        if max_duration is not None:
            max_duration = max_duration // 2

    # Enforce the max duration
    if max_duration:
        if start_token >= max_duration:
            logger.warn(f"Got start time outside of audio boundary")
        else:
            weights[:-1, max_duration:] = worse_weight

    # Encourage to start early
    weights[0, 0] = weights.min()
    weights[0, refine_whisper_precision_nframes * 2 :] = worse_weight

    if subwords_can_be_empty:
        step_pattern = dtw.stepPattern.symmetric1
    else:
        # Similar as "symmetric1" but without the possibility to have the same timestamp for two tokens
        step_pattern = dtw.stepPattern.StepPattern(
            dtw.stepPattern._c(
                1,
                1,
                1,
                -1,
                1,
                0,
                0,
                1,
                2,
                0,
                1,
                -1,
                2,
                0,
                0,
                1,
            )
        )
    alignment = dtw.dtw(weights, step_pattern=step_pattern)

    jumps = np.diff(alignment.index1s)
    jumps = np.pad(jumps, (1, 0), constant_values=1)
    jumps = jumps.astype(bool)
    jumps = alignment.index2s[jumps]
    jumps = np.pad(jumps, (0, 1), constant_values=alignment.index2s[-1])

    jumps_start = jumps
    disfluences = {}
    if detect_disfluencies:
        jumps_start = copy.copy(jumps)

        for i_token, (tok, begin, end) in enumerate(zip(tokens, jumps[:-1], jumps[1:])):
            # Find local maxima in the portion of attention weights
            attention_weights = -weights[i_token, begin:end]
            peaks, properties = find_peaks(
                attention_weights,
                width=3,
                prominence=0.02,
            )
            # If more than
            if len(peaks) > 1:
                if "left_ips" in properties:
                    left = [round(x) for x in properties["left_ips"]]
                else:
                    left = properties["left_bases"]

                new_begin = left[-1] + begin

                jumps_start[i_token] = new_begin

                if new_begin != begin:
                    is_punctuation = (
                        tokenizer.decode_with_timestamps([tok]) in _punctuation
                    )
                    if not is_punctuation:
                        disfluences[i_token] = (begin, jumps_start[i_token])
                    else:
                        disfluences[i_token + 1] = (begin, end)

    # display the word-level timestamps in a table
    word_boundaries = np.cumsum([len(t) for t in word_tokens])
    word_boundaries = np.pad(word_boundaries, (1, 0))
    begin_times = jumps_start[word_boundaries[:-1]]
    end_times = jumps[word_boundaries[1:] - num_punctuations_per_tokens]

    begin_times = begin_times * AUDIO_TIME_PER_TOKEN
    end_times = end_times * AUDIO_TIME_PER_TOKEN

    if detect_disfluencies:
        to_be_added = []
        i_start = 0
        for i_word, toks in enumerate(word_tokens[:-1]):
            i_end = i_start + len(toks)
            if i_start in disfluences and i_word > 0:
                begin, end = disfluences[i_start]
                begin *= AUDIO_TIME_PER_TOKEN
                end *= AUDIO_TIME_PER_TOKEN
                to_be_added.append((i_word, begin, end))
            i_start = i_end
        # Add from the end to avoid messing up the indices
        for i_word, begin, end in to_be_added[-1::-1]:
            words.insert(i_word, DISFLUENCY_MARK)
            word_tokens.insert(i_word, [])
            word_tokens_indices.insert(i_word, [])
            begin_times = np.insert(begin_times, i_word, begin)
            end_times = np.insert(end_times, i_word, end)

    # Ignore start / end tokens
    if not refine_whisper_precision_nframes:
        begin_times[1] = begin_times[0]
    if not refine_whisper_precision_nframes:
        end_times[-2] = end_times[-1]
    if unfinished_decoding:
        words = words[1:]
        word_tokens = word_tokens[1:]
        word_tokens_indices = word_tokens_indices[1:]
        begin_times = begin_times[1:]
        end_times = end_times[1:]
    else:
        words = words[1:-1]
        word_tokens = word_tokens[1:-1]
        word_tokens_indices = word_tokens_indices[1:-1]
        begin_times = begin_times[1:-1]
        end_times = end_times[1:-1]

    return [
        dict(
            text=word,
            start=round(begin + start_time, 2),
            end=round(end + start_time, 2),
            tokens=tokens,
            tokens_indices=tokens_indices,
        )
        for word, begin, end, tokens, tokens_indices in zip(
            words, begin_times, end_times, word_tokens, word_tokens_indices
        )
        if not word.startswith("<|")
    ]


def remove_last_null_duration_words(transcription, words, recompute_text=False):
    """
    Remove words with null duration happening at the end of a chunk (probable Whisper hallucinations)
    """
    # First group segments by audio chunk
    segments_groups = {}
    seek = None
    current_chunk = -1
    for i, segment in enumerate(transcription["segments"]):
        if segment["seek"] != seek:
            current_chunk += 1
            seek = segment["seek"]
        segments_groups[i] = current_chunk

    # Remove words with null duration happening at the end of a chunk
    current_chunk = -1
    is_last_empty = False
    to_remove = []
    for i, word in enumerate(words[::-1]):  # Reverse order
        i = len(words) - i - 1
        empty = word["start"] == word["end"]
        idx_segment = word["idx_segment"]
        group = segments_groups[idx_segment]
        if current_chunk != group:
            is_last_empty = empty
            current_chunk = group
        elif not empty:
            is_last_empty = False
        if is_last_empty:
            # Remove word
            to_remove.append(i)
            # Shorten text of segment
            full_word = "".join(word["tokens"])
            logger.debug(
                f"Removing word {i+1}/{len(words)} \"{full_word}\" with empty duration at the end of segment {idx_segment+1}/{len(transcription['segments'])}"
            )
            segment = transcription["segments"][idx_segment]
            text = segment["text"]
            if not text.endswith(full_word):  # see issue #62
                if text.endswith(full_word[:-1]):
                    full_word = full_word[:-1]
                elif text[:-1].endswith(full_word):
                    text = text[:-1]
                else:
                    raise RuntimeError(f'"{text}" not ending with "{full_word}"')
            text = text[: -len(full_word)]
            if i > 0 and words[i - 1]["idx_segment"] == idx_segment:
                segment["text"] = text
            else:
                logger.debug(f"Removing empty segment {idx_segment}")
                # Remove segment with no more words
                transcription["segments"].pop(idx_segment)
                for j in range(i + 1, len(words)):
                    words[j]["idx_segment"] -= 1
            recompute_text = True

    for i in to_remove:
        words.pop(i)

    if recompute_text:
        transcription["text"] = "".join([s["text"] for s in transcription["segments"]])

    return transcription, words


def find_start_padding(mfcc):
    """Return start of padding given the mfcc, or None if there is no padding."""
    last_mfcc = mfcc[0, :, -1]
    if torch.min(last_mfcc) == torch.max(last_mfcc) == 0:
        candidate_index = mfcc.shape[-1] - 2
        while candidate_index > 0:
            candidate = mfcc[0, :, candidate_index]
            if not torch.equal(candidate, last_mfcc):
                return candidate_index + 1
            candidate_index -= 1
        return 0


_punctuation = (
    "".join(c for c in string.punctuation if c not in ["-", "'"]) + "。，！？：”、…"
)


def split_tokens_on_unicode(
    tokens: List,
    tokenizer,
    remove_punctuation_from_words: bool = False,
    isolate_punctuations: bool = False,
):
    words = []
    word_tokens = []
    word_tokens_indices = []
    current_tokens = []

    for token in tokens:
        current_tokens.append(token)
        decoded = tokenizer.decode_with_timestamps(current_tokens)
        if "\ufffd" not in decoded:
            punctuation = not isolate_punctuations and (
                decoded.strip() and decoded.strip() in _punctuation
            )
            previous_special = len(word_tokens_indices) > 0 and (
                word_tokens_indices[-1][-1] >= tokenizer.eot
            )
            if punctuation and not previous_special:
                if len(words) == 0:
                    words = [""]
                    word_tokens = [[]]
                if not remove_punctuation_from_words:
                    words[-1] += decoded
                word_tokens[-1].append(decoded)
                word_tokens_indices[-1].extend(current_tokens)
            else:
                words.append(decoded)
                word_tokens.append([decoded])
                word_tokens_indices.append(current_tokens)
            current_tokens = []

    return words, word_tokens, word_tokens_indices


def split_tokens_on_spaces(
    tokens: torch.Tensor, tokenizer, remove_punctuation_from_words: bool = False
):
    (
        subwords,
        subword_tokens_list,
        subword_tokens_indices_list,
    ) = split_tokens_on_unicode(
        tokens,
        tokenizer,
        remove_punctuation_from_words=remove_punctuation_from_words,
    )
    words = []
    word_tokens = []
    word_tokens_indices = []

    for i, (subword, subword_tokens, subword_tokens_indices) in enumerate(
        zip(subwords, subword_tokens_list, subword_tokens_indices_list)
    ):
        special = subword_tokens_indices[0] >= tokenizer.eot
        previous_special = (i > 0) and (
            subword_tokens_indices_list[i - 1][0] >= tokenizer.eot
        )
        with_space = subword.startswith(" ")
        punctuation = (subword.strip() and subword.strip()) in _punctuation
        if special or (with_space and not punctuation) or previous_special:
            words.append(subword.strip())
            word_tokens.append(subword_tokens)
            word_tokens_indices.append(subword_tokens_indices)
        else:
            words[-1] = words[-1] + subword.strip()
            word_tokens[-1].extend(subword_tokens)
            word_tokens_indices[-1].extend(subword_tokens_indices)

    return words, word_tokens, word_tokens_indices


def ensure_increasing_positions(segments, min_duration: int = 0):
    """Ensure that "start" and "end" come in increasing order."""
    has_modified_backward = False
    previous_end = 0
    for i, seg in enumerate(segments):
        if seg["start"] < previous_end:
            assert i > 0
            new_start = round((previous_end + seg["start"]) / 2, 2)
            if new_start < segments[i - 1]["start"] + min_duration:
                new_start = previous_end
            else:
                segments[i - 1]["end"] = new_start
                has_modified_backward = True
            seg["start"] = new_start
        if seg["end"] <= seg["start"] + min_duration:
            seg["end"] = seg["start"] + min_duration
        previous_end = seg["end"]
    if has_modified_backward:
        return ensure_increasing_positions(segments, min_duration)

    previous_end = 0
    for seg in segments:
        seg["start"] = round(seg["start"], 2)
        seg["end"] = round(seg["end"], 2)
        assert (
            seg["start"] >= previous_end
        ), f"Got segment {seg} coming before the previous finishes ({previous_end})"
        assert seg["end"] > seg["start"], f"Got segment {seg} with end <= start"
        previous_end = seg["end"]

    return segments


def flatten(list_of_lists, key: str | None = None):
    for sublist in list_of_lists:
        for item in sublist.get(key, []) if key else sublist:
            yield item


def force_cudnn_initialization(device: torch.device | None = None, s: int = 32):
    if device is None:
        device = torch.device("cuda")
    torch.nn.functional.conv2d(
        torch.zeros(s, s, s, s, device=device),
        torch.zeros(s, s, s, s, device=device),
    )


"""
base85-encoded (n_layers, n_heads) boolean arrays indicating the cross-attention heads that are
highly correlated to the word-level timing, i.e. the alignment between audio and text tokens.
"""
_ALIGNMENT_HEADS = {
    "base": b"ABzY8KQ!870{>%RzyTQH3`Q^yNP!>##QT-<FaQ7m",
    "small": b"ABzY8DmU6=0{>%Rpa?J`kvJ6qF(V^F86#Xh7JUGMK}P<N0000",
    "medium": b"ABzY8B0Jh+0{>%R7}kK1fFL7w6%<-Pf*t^=N)Qr&0RR9",
}

_PARAMETERS_TO_MODEL_NAME = {
    71825408: "base.en",
    71825920: "base",
    240582144: "small.en",
    240582912: "small",
    762320896: "medium.en",
    762321920: "medium",
}


def get_alignment_heads(model):
    model_name = _PARAMETERS_TO_MODEL_NAME[_get_number_of_parameters(model)]
    num_layers = model.dims.n_text_layer
    num_heads = model.dims.n_text_head
    return _get_alignment_heads(model_name, num_layers, num_heads)


def _get_alignment_heads(model_name, num_layers, num_heads):
    dump = _ALIGNMENT_HEADS[model_name]
    array = np.frombuffer(gzip.decompress(base64.b85decode(dump)), dtype=bool).copy()
    mask = torch.from_numpy(array).reshape(num_layers, num_heads)
    return mask.to_sparse()


def _get_number_of_parameters(model):
    return sum(p.numel() for p in model.parameters())


def filtered_keys(
    result,
    keys: List = [
        "text",
        "segments",
        "words",
        "language",
        "start",
        "end",
        "confidence",
    ],
):
    if isinstance(result, dict):
        return {k: filtered_keys(v, keys) for k, v in result.items() if k in keys}
    if isinstance(result, list):
        return [filtered_keys(v, keys) for v in result]
    if isinstance(result, float):
        return round(result, 2)
    return result
