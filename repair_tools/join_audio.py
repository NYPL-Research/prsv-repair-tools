# 0. targets part files matching r"p\d\d" in 'data/EditMasters' and 'data/ServiceCopies'; 'data/PreservationMasters' is left alone
# 1. if wav/flac: part file is passed to soundfile import, which pulls file info, ie subtype, sample rate, channels, duration, size
# 2. if mp4: ffprobe gets sample rate, channels, duration, size, and bits per raw sample (defaults to AAC)
# ***** PCM_24 is default for outliers, retains all bits (saw bit loss in previous versions without forcing PCM type) 
# 3. audio is decoded into 32 bit float (keeping source sampling rate)
# 3a. soundfile for wav/flac, ffmpeg for mp4 (lossy vs. not)
# 4. audio floats are compared across max 30s window (slices raw channels before mono average to save RAM)
# 4a. NCC: scipy.correlate uses correlation score to test every sample from p1 against p2
# ---- r(t) = [(p1 wave height)*(p2 wave height)/(total energy or volume)] 
# ---- r(t) > 0.60 [min_corr], acoustic overlap of t samples, if near 0, files more closely cut
# 4b. GCC-PHAT (FFT) used when there is too much background noise, can confuse NCC and throw off correlation
# 4c. RMS gain ratio calculated across overlap to detect volume differences between parts
# 5. parts with overlap and a good correlation score are stitched using equal-power crossfade and gain matching
# 5a. pre-allocates output array once to prevent memory spikes; frees intermediate audio buffers between parts
# 5b. uses numpy: cos^2 + sin^2 = 1 comparison against sample rates along overlap to keep acoustic energy constant
# 5c. gain matching lowers louder part or boosts quieter part to prevent clipping (bypassed if equal or --no-match-gain)
# 6. joined parts saved as 24-bit (force PCM_24 to avoid 16-bit default in soundfile; format="RF64" for large WAVs); joined array cleared from RAM
# 7. Validation has 3 tests (uses soundfile frame-seeking for wav/flac to avoid reloading multi-GB files into RAM):
# 7a. (1) total length of the stitched file == [sum(all parts) - crossfaded dupe samples]
# ---- WAV/FLAC: must be exact (verified via file header frames
# ---- MP4: up to 4096 padding samples allowed (could be AAC encoder delay)
# 7b. (2) SNR: audio before and after crossfade should be the same (999.0 == identical, same as 0 difference for error/distortion)
# ---- evaluated on first 5s of p01 and last 5s of final part via seek
# 7c. (3) 200 ms is sampled at join seam, checking for abnormal/non-gradual jumps in wave length to indicate pops or clicks
# ---- if delta s[n] > 0.40, not a gradual change in wave height; checks for energy dropouts (RMS < 1e-4)
# 8. if valid, output file goes to same location as source part files (stripping 'p##'); source parts are kept
# 9. logging: terminal logger tracks processing stages; file logger writes audit table to {pkg}_{location}_joinaudiolog_{DDMMYYYY}.log
# optional / flags:
# 10. service copy files joined by default (use --no-servicecopies / --no-sc to disable)
# 11. merge-json (-mj) will compare part file jsons & fill in technical specs based on new combined file
# 12. --no-match-gain disables automatic RMS gain balancing

import argparse
import datetime
import logging
import re
import json
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import correlate, correlation_lags


VALID_MEDIA_EXTS = {".mp4", ".wav", ".flac"}


def setup_terminal_logger():
    logger = logging.getLogger("join_parts")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter("%(asctime)s [%(levelname)s]: %(message)s")
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)

    return logger


logger = setup_terminal_logger()


def get_file_logger(log_file_path):
    """File handler for writing audit tables."""
    file_logger = logging.getLogger(f"file_logger_{log_file_path}")
    file_logger.setLevel(logging.INFO)
    file_logger.handlers.clear()
    file_logger.propagate = False

    file_handler = logging.FileHandler(log_file_path, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_format = logging.Formatter("%(message)s")
    file_handler.setFormatter(file_format)
    file_logger.addHandler(file_handler)

    return file_logger

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--package",
        "-p",
        type=Path,
        nargs="+",
        help="Path to one or more bags.",
    )
    parser.add_argument(
        "--directory",
        "-d",
        type=Path,
        nargs="+",
        help="Path to directory containing package bags.",
    )
    parser.add_argument(
        "--no-servicecopies",
        "--no-sc",
        action="store_true",
        help="Disable automatic joining of service copy part files in ServiceCopies.",
    )
    parser.add_argument(
        "--merge-json",
        "-mj",
        action="store_true",
        help="Flag to compare and merge part JSON files for joined parts.",
    )
    parser.add_argument(
        "--crossfade",
        type=float,
        default=0.1,
        help="Crossfade duration in secs for overlapping joins (default: 0.1).",
    )
    parser.add_argument(
        "--min-corr",
        type=float,
        default=0.60,
        help="Minimum normalized cross-correlation confidence threshold (default: 0.60).",
    )
    parser.add_argument(
        "--match-gain",
        action="store_true",
        default=True,
        help="Match volume levels between parts using RMS ratio (default: True).",
    )
    parser.add_argument(
        "--no-match-gain",
        action="store_false",
        dest="match_gain",
        help="Disable automatic gain matching.",
    )
    return parser.parse_args()

def format_duration_human(total_seconds):
    """Formats seconds into standard archival timecode: HH:MM:SS.mmm."""
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = int(total_seconds % 60)
    millis = int(round((total_seconds - int(total_seconds)) * 1000))
    if millis >= 1000:
        millis -= 1000
        seconds += 1
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def format_bytes_human(num_bytes):
    """Format byte counts into a string with GB/MB scale for audit table."""
    gb = num_bytes / (1024 ** 3)
    mb = num_bytes / (1024 ** 2)
    if gb >= 1.0:
        return f"{num_bytes:,} ({gb:.2f} GB)"
    return f"{num_bytes:,} ({mb:.2f} MB)"


def get_file_metadata(file_path):
    """Retrieves native sample rate, channels, and bit depth subtype."""
    path_obj = Path(file_path)
    ext = path_obj.suffix.lower()
    file_size = path_obj.stat().st_size

    if ext in [".wav", ".flac"]:
        info = sf.info(str(path_obj))
        # default PCM_24 if 24-bit or float, or retain subtype (preserve all bits)
        subtype = info.subtype if info.subtype in ["PCM_16", "PCM_24", "PCM_32"] else "PCM_24"
        return info.samplerate, info.channels, subtype, float(info.duration), file_size

    # get mp4 info via ffprobe
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate,channels,bits_per_raw_sample:format=duration",
        "-of", "csv=p=0:s=,",
        str(path_obj),
    ]
    try:
        res = subprocess.check_output(cmd).decode().strip().split(",")
        sr = int(res[0])
        ch = int(res[1])
        bits = res[2] if len(res) > 2 and res[2] != "N/A" and res[2] != "" else "16"
        subtype = "PCM_24" if bits == "24" else "AAC"
        dur = float(res[3]) if len(res) > 3 else 0.0
        return sr, ch, subtype, dur, file_size
    except Exception:
        return 44100, 2, "AAC", 0.0, file_size


def audio_to_float(file_path, target_sr=None):
    """Decodes audio into float32 preserving native sample rate."""
    path_obj = Path(file_path)
    if not path_obj.exists():
        raise FileNotFoundError(f"Missing file: {path_obj}")

    ext = path_obj.suffix.lower()

    # direct read for lossless formats (avoid bit loss)
    if ext in [".wav", ".flac"]:
        data, sr = sf.read(str(path_obj), dtype="float32")
        if data.ndim == 1:
            data = data[:, np.newaxis]
        return data, sr

    # ffmpeg pipe fallback for mp4 containers
    sr, ch, _, _, _ = get_file_metadata(path_obj)
    use_sr = target_sr if target_sr is not None else sr

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-threads", "0",
        "-i", str(path_obj),
        "-vn",
        "-f", "f32le",
        "-acodec", "pcm_f32le",
        "-ar", str(use_sr),
        "-ac", str(ch),
        "-",
    ]

    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    raw_bytes, _ = process.communicate()

    if process.returncode != 0 or len(raw_bytes) == 0:
        raise RuntimeError(f"FFmpeg failed to decode audio from {path_obj.name}")

    audio_data = np.frombuffer(raw_bytes, dtype=np.float32)
    return audio_data.reshape(-1, ch), use_sr


def save_audio(output_path, audio_data, sr, subtype="PCM_24"):
    """Saves audio preserving 24-bit depth and enabling RF64 for large WAV files."""
    out_obj = Path(output_path)
    ext = out_obj.suffix.lower()
    fmt = "RF64" if ext == ".wav" else None

    if ext in [".wav", ".flac"]:
        # force PCM_24 to keep soundfile from defaulting flac files to 16-bit
        sf.write(str(out_obj), audio_data, sr, subtype=subtype, format=fmt)
    elif ext == ".mp4":
        cmd = [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-f", "f32le",
            "-ar", str(sr),
            "-ac", str(audio_data.shape[1] if audio_data.ndim > 1 else 1),
            "-i", "-",
            "-c:a", "aac",
            "-b:a", "320k",
            str(out_obj),
        ]
        process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        process.communicate(audio_data.astype(np.float32).tobytes())
        if process.returncode != 0:
            raise RuntimeError(f"FFmpeg failed to encode {out_obj.name}")
    else:
        sf.write(str(out_obj), audio_data, sr, subtype=subtype, format=fmt)


def find_part_overlap(audio1, audio2, sr, max_search_sec=30.0, min_corr=0.60):
# standard NCC with GCC-PHAT fallback.
    search_samples = int(max_search_sec * sr)

    # slice raw channels first to avoid memory issues 
    tail1_raw = audio1[-min(len(audio1), search_samples):]
    head2_raw = audio2[:min(len(audio2), search_samples)]

    m1 = np.mean(tail1_raw, axis=1)
    m2 = np.mean(head2_raw, axis=1)

    tail1 = m1
    head2 = m2

    t1_norm = tail1 - np.mean(tail1)
    h2_norm = head2 - np.mean(head2)

    denom = np.linalg.norm(t1_norm) * np.linalg.norm(h2_norm)
    if denom == 0:
        return 0, 0.0, 1.0
    
    # scipy.signal.correlate used == Convolution Theorem (Wiener-Khinchin theorem)
    # FFT of both signals: X(f) = F{x(t)} and Y(f) = F{y(t)}
    # mutliply freq sepctrum of one by the complex conjugate(?) of the other in the freq domain: S_xy(f) = X(F)*Y^*(f)
    # compute inverse FFT back to the time domain: R_xy(t) = F^-1{S_xy(f)}
    raw_corr = correlate(t1_norm, h2_norm, mode="full")
    lags = correlation_lags(len(t1_norm), len(h2_norm), mode="full")

    valid_mask = lags >= 0
    valid_corr = raw_corr[valid_mask]
    valid_lags = lags[valid_mask]

    overlap_samples = 0
    confidence = 0.0

    if len(valid_corr) > 0:
        best_idx = np.argmax(valid_corr)
        best_lag = valid_lags[best_idx]
        cand_overlap = len(t1_norm) - best_lag

        if cand_overlap > 0:
            sub_a = tail1[-cand_overlap:]
            sub_b = head2[:cand_overlap]
            sa_c = sub_a - np.mean(sub_a)
            sb_c = sub_b - np.mean(sub_b)
            d = np.linalg.norm(sa_c) * np.linalg.norm(sb_c)
            confidence = float(np.dot(sa_c, sb_c) / d) if d > 0 else 0.0
            overlap_samples = cand_overlap

    # NCC does not meet confidence
    if confidence < min_corr:
        # GCC-PHAT fallback (aka generalized cross-correlation with phase transform)
        n = len(t1_norm) + len(h2_norm)
        fft_tail = np.fft.rfft(t1_norm, n=n)
        fft_head = np.fft.rfft(h2_norm, n=n)

        cross_spectrum = fft_tail * np.conj(fft_head)
        mag = np.abs(cross_spectrum)
        mag[mag < 1e-6] = 1e-6  # prevent div by zero

        phat_corr = np.fft.irfft(cross_spectrum / mag, n=n)

        # valid positive overlap range corresponds to indices [1, len(t1_norm)]
        valid_phat = phat_corr[:len(t1_norm)]
        phat_lag = int(np.argmax(valid_phat))
        phat_overlap = len(t1_norm) - phat_lag

        if phat_overlap > 0:
            sub_a = tail1[-phat_overlap:]
            sub_b = head2[:phat_overlap]
            sa_c = sub_a - np.mean(sub_a)
            sb_c = sub_b - np.mean(sub_b)
            d = np.linalg.norm(sa_c) * np.linalg.norm(sb_c)
            phat_conf = float(np.dot(sa_c, sb_c) / d) if d > 0 else 0.0

            if phat_conf >= min_corr:
                confidence = phat_conf
                overlap_samples = phat_overlap

    # RMS Gain Ratio/ overlap
    gain_ratio = 1.0
    if overlap_samples > 0:
        sub_a = tail1[-overlap_samples:]
        sub_b = head2[:overlap_samples]
        rms_a = np.sqrt(np.mean(sub_a ** 2))
        rms_b = np.sqrt(np.mean(sub_b ** 2))
        if rms_b > 1e-6:
            gain_ratio = float(rms_a / rms_b)

    return (
        overlap_samples if confidence >= min_corr else 0,
        confidence,
        gain_ratio,
    )


def stitch_parts(audio1, audio2, overlap_samples, sr, crossfade_sec=0.1, gain_ratio=1.0, match_gain=True):
    """Stitches audio parts using equal-power crossfading and gain matching."""
    if (not match_gain) or (abs(1.0 - gain_ratio) < 1e-4):
        g1, g2 = 1.0, 1.0
    elif gain_ratio > 1.0:
        # p1 is louder; reduce p1 to match p2 
        g1 = 1.0 / gain_ratio
        g2 = 1.0
    else:
        # p2 is louder; reduce p2 to match p1 
        g1 = 1.0
        g2 = gain_ratio

    # determines output array once (helps w/ memory issues)
    total_len = len(audio1) + len(audio2) - overlap_samples
    out = np.empty((total_len, audio1.shape[1]), dtype=np.float32)

    cut = len(audio1) - overlap_samples

    # 1. p1 unique audio
    if g1 != 1.0:
        out[:cut] = audio1[:cut] * g1
    else:
        out[:cut] = audio1[:cut]

    # 2. overlap transition
    if overlap_samples > 0:
        fade_len = min(overlap_samples, int(crossfade_sec * sr))
        t = np.linspace(0, np.pi / 2, fade_len)[:, np.newaxis]
        fade_out = np.cos(t)
        fade_in = np.sin(t)

        ov_a = audio1[-overlap_samples:] * g1
        ov_b = audio2[:overlap_samples] * g2

        out[cut : cut + fade_len] = (ov_a[:fade_len] * fade_out) + (ov_b[:fade_len] * fade_in)
        out[cut + fade_len : cut + overlap_samples] = ov_b[fade_len:]

    # 3. p2 unique audio
    if g2 != 1.0:
        out[cut + overlap_samples:] = audio2[overlap_samples:] * g2
    else:
        out[cut + overlap_samples:] = audio2[overlap_samples:]

    return out


def calculate_snr(reference, degraded):
    """Calcs Signal-to-Noise Ratio in dB."""
    min_len = min(len(reference), len(degraded))
    ref = reference[:min_len]
    deg = degraded[:min_len]

    signal_pwr = np.sum(ref ** 2)
    noise_pwr = np.sum((ref - deg) ** 2)

    if noise_pwr == 0:
        return 999.0  # identical bit-for-bit
    if signal_pwr == 0:
        return 0.0
    return float(10 * np.log10(signal_pwr / noise_pwr))


def read_window_mono(sf_file, start_frame, num_frames):
    """Reads a precise window without loading the full file into memory."""
    start = max(0, start_frame)
    count = min(num_frames, sf_file.frames - start)
    if count <= 0:
        return np.array([], dtype=np.float32)
    sf_file.seek(start)
    data = sf_file.read(count, dtype="float32")
    return np.mean(data, axis=1) if data.ndim > 1 else data


def validate_stitched_output(part_files, stitched_path, overlaps_samples, sr):
    # switched from full decoding to more memory efficient logic
    is_mp4 = stitched_path.suffix.lower() == ".mp4"

    # mp4 fallback (AAC container validation)
    if is_mp4:
        stitched_audio, _ = audio_to_float(stitched_path, target_sr=sr)
        stitched_mono = np.mean(stitched_audio, axis=1)
        stitched_len = len(stitched_mono)

        parts_mono = []
        total_part_samples = 0
        for pf in part_files:
            audio, _ = audio_to_float(pf, target_sr=sr)
            mono = np.mean(audio, axis=1)
            parts_mono.append(mono)
            total_part_samples += len(mono)

        total_overlap = sum(overlaps_samples)
        expected_samples = total_part_samples - total_overlap
        sample_diff = abs(stitched_len - expected_samples)
        # lossy container packing can introducec 1024-2048 delay/padding frames
        duration_passed = sample_diff <= 4096
        # signal fidelity check
        head_len = min(len(parts_mono[0]) - (overlaps_samples[0] if overlaps_samples else 0), int(5 * sr))
        head_ref = parts_mono[0][:head_len] if head_len > 0 else np.array([])
        head_deg = stitched_mono[:head_len] if head_len > 0 else np.array([])
        head_snr = calculate_snr(head_ref, head_deg) if head_len > 0 else 999.0

        tail_len = min(len(parts_mono[-1]), int(5 * sr))
        tail_ref = parts_mono[-1][-tail_len:] if tail_len > 0 else np.array([])
        tail_deg = stitched_mono[-tail_len:] if tail_len > 0 else np.array([])
        tail_snr = calculate_snr(tail_ref, tail_deg) if tail_len > 0 else 999.0

        # gain-matching verification notes
        head_note = ""
        if head_snr <= 60.0 and len(head_ref) > 0:
            pwr = np.sum(head_ref ** 2)
            if pwr > 0:
                scale = np.sum(head_ref * head_deg) / pwr
                if scale > 0 and calculate_snr(head_ref * scale, head_deg) > 60.0:
                    gain_db = 20 * np.log10(scale)
                    head_note = f" (Note: {gain_db:+.2f} dB shift from gain matching; waveform shape identical)"

        tail_note = ""
        if tail_snr <= 60.0 and len(tail_ref) > 0:
            pwr = np.sum(tail_ref ** 2)
            if pwr > 0:
                scale = np.sum(tail_ref * tail_deg) / pwr
                if scale > 0 and calculate_snr(tail_ref * scale, tail_deg) > 60.0:
                    gain_db = 20 * np.log10(scale)
                    tail_note = f" (Note: {gain_db:+.2f} dB shift from gain matching; waveform shape identical)"

        current_offset = 0
        issues_detected = 0
        seam_metrics = []
        for i, overlap in enumerate(overlaps_samples):
            seam_center = current_offset + len(parts_mono[i]) - overlap
            current_offset += len(parts_mono[i]) - overlap

            w_start = max(0, seam_center - int(0.1 * sr))
            w_end = min(stitched_len, seam_center + int(0.1 * sr))
            seam_window = stitched_mono[w_start:w_end]

            diffs = np.abs(np.diff(seam_window))
            max_jump = float(np.max(diffs)) if len(diffs) > 0 else 0.0
            rms = float(np.sqrt(np.mean(seam_window ** 2)))

            status = "SMOOTH"
            if max_jump > 0.40 or rms < 1e-4:
                status = "WARNING"
                issues_detected += 1
            seam_metrics.append((max_jump, status))

        all_passed = duration_passed and (issues_detected == 0)
        return all_passed, {
            "expected_samples": expected_samples,
            "actual_samples": stitched_len,
            "sample_diff": sample_diff,
            "duration_passed": duration_passed,
            "head_snr": head_snr,
            "tail_snr": tail_snr,
            "head_note": head_note,
            "tail_note": tail_note,
            "seam_metrics": seam_metrics,
            "all_passed": all_passed,
        }

    # wav/flac
    with sf.SoundFile(str(stitched_path)) as out_f:
        stitched_len = out_f.frames

        part_lengths = []
        for pf in part_files:
            with sf.SoundFile(str(pf)) as pf_f:
                part_lengths.append(pf_f.frames)

        total_part_samples = sum(part_lengths)
        expected_samples = total_part_samples - sum(overlaps_samples)
        sample_diff = abs(stitched_len - expected_samples)

        # snr head (firstt 5 seconds)
        with sf.SoundFile(str(part_files[0])) as p1_f:
            head_len = min(part_lengths[0] - (overlaps_samples[0] if overlaps_samples else 0), int(5 * sr))
            head_ref = read_window_mono(p1_f, 0, head_len)
            head_deg = read_window_mono(out_f, 0, head_len)
            head_snr = calculate_snr(head_ref, head_deg) if head_len > 0 else 999.0

        # snr tail (last 5 seconds)
        with sf.SoundFile(str(part_files[-1])) as plast_f:
            tail_len = min(part_lengths[-1], int(5 * sr))
            tail_ref = read_window_mono(plast_f, part_lengths[-1] - tail_len, tail_len)
            tail_deg = read_window_mono(out_f, stitched_len - tail_len, tail_len)
            tail_snr = calculate_snr(tail_ref, tail_deg) if tail_len > 0 else 999.0

        # gain-matching verification notes
        head_note = ""
        if head_snr <= 60.0 and len(head_ref) > 0:
            pwr = np.sum(head_ref ** 2)
            if pwr > 0:
                scale = np.sum(head_ref * head_deg) / pwr
                if scale > 0 and calculate_snr(head_ref * scale, head_deg) > 60.0:
                    gain_db = 20 * np.log10(scale)
                    head_note = f" (Note: {gain_db:+.2f} dB shift from gain matching; waveform shape identical)"

        tail_note = ""
        if tail_snr <= 60.0 and len(tail_ref) > 0:
            pwr = np.sum(tail_ref ** 2)
            if pwr > 0:
                scale = np.sum(tail_ref * tail_deg) / pwr
                if scale > 0 and calculate_snr(tail_ref * scale, tail_deg) > 60.0:
                    gain_db = 20 * np.log10(scale)
                    tail_note = f" (Note: {gain_db:+.2f} dB shift from gain matching; waveform shape identical)"

        # seam check
        current_offset = 0
        issues_detected = 0
        seam_metrics = []
        window_size = int(0.1 * sr)

        for i, overlap in enumerate(overlaps_samples):
            seam_center = current_offset + part_lengths[i] - overlap
            current_offset += part_lengths[i] - overlap

            w_start = max(0, seam_center - window_size)
            seam_window = read_window_mono(out_f, w_start, window_size * 2)

            diffs = np.abs(np.diff(seam_window))
            max_jump = float(np.max(diffs)) if len(diffs) > 0 else 0.0
            rms = float(np.sqrt(np.mean(seam_window ** 2)))

            status = "SMOOTH"
            if max_jump > 0.40 or rms < 1e-4:
                status = "WARNING"
                issues_detected += 1
            seam_metrics.append((max_jump, status))

    duration_passed = sample_diff <= 0
    all_passed = duration_passed and (issues_detected == 0)
    return all_passed, {
        "expected_samples": expected_samples,
        "actual_samples": stitched_len,
        "sample_diff": sample_diff,
        "duration_passed": duration_passed,
        "head_snr": head_snr,
        "tail_snr": tail_snr,
        "head_note": head_note,
        "tail_note": tail_note,
        "seam_metrics": seam_metrics,
        "all_passed": all_passed,
    }


def get_file_location(path):
    parent_name = path.parent.name.lower()
    name_lower = path.name.lower()

    if parent_name == "preservationmasters" or "_pm" in name_lower:
        return None

    #double check for _em mp4 files in service copies folder 
    if parent_name == "servicecopies" or "_sc" in name_lower:
        return "sc"

    if parent_name == "editmasters" or "_em" in name_lower:
        return "em"

    return None


def find_part_files(package_path, include_service_copies=True):
    data_path = package_path / "data"
    if not data_path.exists():
        data_path = package_path

    discovered_files = []
    for f in data_path.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in VALID_MEDIA_EXTS:
            continue
        if not re.search(r"p\d\d", f.name):
            continue

        location = get_file_location(f)
        if location == "em":
            discovered_files.append((f, location))
        elif location == "sc" and include_service_copies:
            discovered_files.append((f, location))

    return discovered_files


def group_part_files(part_file_tuples):
    groups = {}

    for f, location in part_file_tuples:
        match = re.search(r"p(\d\d)", f.name)
        if not match:
            continue

        part_num = int(match.group(1))
        dest_filename = re.sub(r"p\d\d", "", f.name)
        output_file_path = f.parent / dest_filename

        if output_file_path not in groups:
            groups[output_file_path] = {"location": location, "parts": []}
        groups[output_file_path]["parts"].append((part_num, f))

    for out_path in groups:
        groups[out_path]["parts"].sort(key=lambda item: item[0])
        groups[out_path]["parts"] = [item[1] for item in groups[out_path]["parts"]]

    return groups

def merge_jsons(part_json_paths, joined_media_path, output_json_path):
    if not part_json_paths:
        return

    loaded_jsons = []
    for path in part_json_paths:
        with open(path, "r", encoding="utf-8") as f:
            loaded_jsons.append(json.load(f))

    base_json = loaded_jsons[0].copy()

    tech_keys = {
        "filename", "fileName", "file_name", "fileSize", "file_size",
        "duration", "durationMilli", "durationHuman", "sampleRate", "channels"
    }
    for i, other in enumerate(loaded_jsons[1:], start=2):
        for key in base_json.keys() & other.keys():
            if key not in tech_keys and base_json[key] != other[key]:
                logger.warning(
                    f"JSON metadata mismatch in part {i} for '{key}': "
                    f"'{base_json[key]}' != '{other[key]}'"
                )

    sr, ch, _, dur_sec, file_size = get_file_metadata(joined_media_path)

    updates = {
        "filename": joined_media_path.name,
        "fileName": joined_media_path.name,
        "fileSize": file_size,
        "duration": dur_sec,
        "durationMilli": int(round(dur_sec * 1000)),
        "durationHuman": format_duration_human(dur_sec),
        "sampleRate": sr,
        "channels": ch,
    }

    for k, v in updates.items():
        if k in base_json:
            base_json[k] = v

    if "technical" in base_json and isinstance(base_json["technical"], dict):
        for k, v in updates.items():
            if k in base_json["technical"]:
                base_json["technical"][k] = v

    # 5. Write finalized JSON
    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(base_json, f, indent=2, ensure_ascii=False)
        f.write("\n")

# **table == gemini generated, double check
def generate_audit_table(
    pkg_title,
    location,
    log_file_name,
    source_meta_list,
    seam_data_list,
    output_meta,
    val_summary,
):
    location_label = "EditMasters (em)" if location == "em" else "ServiceCopies (sc)"
    timestamp_iso = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    divider = "=" * 80
    sub_divider = "-" * 80

    lines = [
        divider,
        f"PACKAGE PROCESSING: {pkg_title}",
        f"Folder: {location_label}",
        f"Timestamp:   {timestamp_iso}",
        f"Log File:    {log_file_name}",
        divider,
        "",
        sub_divider,
        "1. SOURCE PARTS",
        f"{'Part Filename':<29} {'Format':<11} {'Sample / Ch':<15} {'Duration':<14} {'Size':<8}",
        sub_divider,
    ]

    for meta in source_meta_list:
        # Compact format & bit-depth (e.g., 'FLAC (24b)' or 'AAC')
        sub = "24b" if "24" in meta["subtype"] else ("16b" if "16" in meta["subtype"] else meta["subtype"])
        fmt_str = f"{meta['format']} ({sub})" if meta["format"] != sub else meta["format"]

        # Compact sample rate and channels (e.g., '96,000 / 2ch')
        sr_val = meta["sr"].replace(" Hz", "").strip()
        sr_ch_str = f"{sr_val} / {meta['ch']}ch"

        # Compact size (e.g., '2.01 GB')
        size_str = meta["size"].split("(")[-1].replace(")", "").strip() if "(" in meta["size"] else meta["size"]

        lines.append(
            f"{meta['filename']:<29} {fmt_str:<11} {sr_ch_str:<15} {meta['duration']:<14} {size_str:<8}"
        )

    lines.extend([
        sub_divider,
        "",
        "2. TRANSITION SEAMS & OVERLAPS",
        f"{'Boundary':<12} {'Overlap':<14} {'Conf':<6} {'Gain Adjustment':<24} {'Jump':<9} {'Status':<7}",
        sub_divider,
    ])

    gain_summary_notes = []

    for seam in seam_data_list:
        boundary = seam.get("boundary", "Transition")
        overlap_cnt = seam.get("overlap", 0)

        # Compact overlap string (e.g., '6.685s (642k)' or '0s (Hard)')
        if overlap_cnt > 0:
            ov_sec = seam.get("overlap_sec", "0.0000 s").replace(" s", "s").strip()
            ov_display = f"{float(ov_sec[:-1]):.3f}s ({overlap_cnt // 1000}k)"
        else:
            ov_display = "0s (Hard Cut)"

        # Parse which file was attenuated and by how much
        gain_str = seam.get("gain_str", "1.000 (+0.00 dB)")
        match_b = re.search(r"p(\d+)\s*->\s*p(\d+)", boundary)
        p_a = f"p{match_b.group(1)}" if match_b else "Part A"
        p_b = f"p{match_b.group(2)}" if match_b else "Part B"

        match_g = re.search(r"([\d\.]+)\s*\(([+-]?[\d\.]+)\s*dB\)", gain_str)
        if match_g:
            ratio = float(match_g.group(1))
            db = float(match_g.group(2))
            if abs(ratio - 1.0) < 1e-4 or abs(db) < 0.01:
                table_gain = "None (0.00 dB)"
                summary_gain = f"{boundary}: Levels matched within tolerance (unity gain)"
            elif ratio > 1.0:
                table_gain = f"pre-{p_b}: -{abs(db):.2f} dB"
                summary_gain = f"{boundary}: {p_a} (and preceding audio) attenuated by -{abs(db):.2f} dB to match {p_b}"
            else:
                table_gain = f"{p_b}: -{abs(db):.2f} dB"
                summary_gain = f"{boundary}: {p_b} attenuated by -{abs(db):.2f} dB to match {p_a}"
        else:
            table_gain = "None (0.00 dB)"
            summary_gain = f"{boundary}: Unity gain"

        gain_summary_notes.append(summary_gain)

        # Compact jump string (e.g., '0.0241')
        raw_jump = seam.get("jump", "0.0000")
        jump_val = raw_jump.split()[0] if " " in raw_jump else raw_jump

        lines.append(
            f"{boundary:<12} {ov_display:<14} {seam.get('confidence', 'N/A'):<6} "
            f"{table_gain:<24} {jump_val:<9} {seam.get('status', 'N/A'):<7}"
        )

    out_status_tag = "PASSED" if val_summary["all_passed"] else "FAILED"
    out_sub = "24b" if "24" in output_meta["subtype"] else ("16b" if "16" in output_meta["subtype"] else output_meta["subtype"])
    out_fmt_str = f"{output_meta['format']} ({out_sub})" if output_meta["format"] != out_sub else output_meta["format"]
    out_sr_val = output_meta["sr"].replace(" Hz", "").strip()
    out_sr_ch_str = f"{out_sr_val} / {output_meta['ch']}ch"

    lines.extend([
        sub_divider,
        "",
        "3. FINAL JOINED SPECIFICATIONS",
        f"{'Output Filename':<29} {'Format':<11} {'Sample / Ch':<15} {'Duration':<14} {'QC':<8}",
        sub_divider,
        f"{output_meta['filename']:<29} {out_fmt_str:<11} {out_sr_ch_str:<15} {output_meta['duration']:<14} {out_status_tag:<8}",
        sub_divider,
        "",
        "Validation Checks:",
        f"  * Sample Budget: Expected {val_summary['expected_samples']:,} vs Actual {val_summary['actual_samples']:,} samples "
        f"(Diff: {val_summary['sample_diff']} samples) -> {'PASS' if val_summary['duration_passed'] else 'FAIL'}",
        f"  * Head SNR (p01 Start):   {val_summary['head_snr']:.2f} dB -> {'PASS' if val_summary['head_snr'] > 60.0 else 'WARN'}{val_summary.get('head_note', '')}",
        f"  * Tail SNR (Last End):   {val_summary['tail_snr']:.2f} dB -> {'PASS' if val_summary['tail_snr'] > 60.0 else 'WARN'}{val_summary.get('tail_note', '')}",
        "  * Gain Matching Action:",
    ])

    for g_note in gain_summary_notes:
        lines.append(f"      - {g_note}")

    lines.extend([
        divider,
        "\n",
    ])

    return "\n".join(lines)

def process_bag(
    package_path,
    crossfade_sec=0.1,
    min_corr=0.60,
    include_service_copies=True,
    merge_json_enabled=False,
    match_gain=True,
):
    pkg_dir = Path(package_path)
    part_files = find_part_files(pkg_dir, include_service_copies=include_service_copies)

    if not part_files:
        logger.info(f"Skipping {pkg_dir.name}: No matching EditMasters or ServiceCopies parts found.")
        return

    grouped_sequences = group_part_files(part_files)
    logger.info(f"Package: {pkg_dir.name} | Sequences found to process: {len(grouped_sequences)}")

    for output_file, seq_info in grouped_sequences.items():
        files = seq_info["parts"]
        location = seq_info["location"]
        role_label = location.upper()

        # log file path: {pkg_title}_{location?}_joinaudiolog_{DDMMYYYY}.log
        date_stamp = datetime.datetime.now().strftime("%d%m%Y")
        log_file_name = f"{pkg_dir.name}_{location}_joinaudiolog_{date_stamp}.log"
        log_file_path = output_file.parent / log_file_name
        audit_logger = get_file_logger(log_file_path)

        logger.info(f"Processing sequence: {output_file.name} ({len(files)} parts) in location '{location}'")

        if output_file.exists():
            logger.warning(f"Joined file already exist: {output_file.name}. Skipping.")
            continue

        # 1. get all source part metadata
        source_meta_list = []
        for pf in files:
            sr_i, ch_i, sub_i, dur_i, size_i = get_file_metadata(pf)
            source_meta_list.append({
                "filename": pf.name,
                "role": role_label,
                "format": pf.suffix.lstrip(".").upper(),
                "subtype": sub_i,
                "sr": f"{sr_i:,} Hz",
                "ch": str(ch_i),
                "duration": format_duration_human(dur_i),
                "size": format_bytes_human(size_i),
            })

        # 2. decode initial file preserving attr
        native_sr, native_ch, native_subtype, _, _ = get_file_metadata(files[0])
        current_audio, sr = audio_to_float(files[0], target_sr=native_sr)
        print(f"Initial: {files[0].name} ({len(current_audio)/sr:.2f}s)")

        overlaps_history = []
        seam_data_list = []

        # 3. stitch parts
        for i in range(len(files) - 1):
            next_file = files[i + 1]
            next_audio, _ = audio_to_float(next_file, target_sr=sr)
            overlap, conf, gain_ratio = find_part_overlap(
                current_audio, next_audio, sr, min_corr=min_corr
            )

            gain_db = 20 * np.log10(gain_ratio) if gain_ratio > 0 else 0.0
            gain_str = f"{gain_ratio:.3f} ({gain_db:+.2f} dB)"
            overlap_sec_str = f"{overlap / sr:.4f} s"

            if overlap > 0:
                logger.info(f"  -> Join p{i+1:02d} -> p{i+2:02d}: Overlap {overlap} samples ({overlap_sec_str}), Conf: {conf:.3f}, Gain: {gain_str}")
            else:
                logger.info(f"  -> Join p{i+1:02d} -> p{i+2:02d}: Hard Cut (No overlap), Conf: {conf:.3f}")

            new_audio = stitch_parts(
                current_audio,
                next_audio,
                overlap,
                sr,
                crossfade_sec=crossfade_sec,
                gain_ratio=gain_ratio,
                match_gain=match_gain,
            )
            overlaps_history.append(overlap)

            # clear RAM before next part
            del current_audio
            del next_audio
            current_audio = new_audio

            seam_data_list.append({
                "boundary": f"p{i+1:02d} -> p{i+2:02d}",
                "overlap": overlap,
                "overlap_sec": overlap_sec_str,
                "confidence": f"{conf:.3f}",
                "gain_str": gain_str,
            })

        # 4. save output
        save_audio(output_file, current_audio, sr, subtype=native_subtype)
        logger.info(f"Successfully saved joined file: {output_file.name}")
        
        # clear RAM again before validation
        del current_audio

        # 5. validation checks
        all_passed, val_summary = validate_stitched_output(files, output_file, overlaps_history, sr)

        # add seam metrics into audit data
        for i, (jump, status) in enumerate(val_summary["seam_metrics"]):
            seam_data_list[i]["jump"] = f"{jump:.4f} ({'Pass' if jump <= 0.40 else 'Warn'})"
            seam_data_list[i]["status"] = status

        # 6. get finalized output metadata
        out_sr, out_ch, out_sub, out_dur, out_sz = get_file_metadata(output_file)
        fmt_label = output_file.suffix.lstrip(".").upper()
        if fmt_label == "WAV":
            fmt_label = "WAV (RF64)"

        output_meta = {
            "filename": output_file.name,
            "role": role_label,
            "format": fmt_label,
            "subtype": out_sub,
            "sr": f"{out_sr:,} Hz",
            "ch": str(out_ch),
            "duration": format_duration_human(out_dur),
            "size": format_bytes_human(out_sz),
        }

        # 7. write table to logfile
        audit_table_text = generate_audit_table(
            pkg_title=pkg_dir.name,
            location=location,
            log_file_name=log_file_name,
            source_meta_list=source_meta_list,
            seam_data_list=seam_data_list,
            output_meta=output_meta,
            val_summary=val_summary,
        )
        audit_logger.info(audit_table_text)
        logger.info(f"Audit table logged to: {log_file_path.name}")

        if all_passed:
            logger.info(f"[PASS] QC validated for {output_file.name}")
        else:
            logger.warning(f"[FAIL] QC failed for {output_file.name}")

        # 8. json merging
        if merge_json_enabled:
            part_jsons = [f.with_suffix(".json") for f in files if f.with_suffix(".json").exists()]
            if part_jsons:
                target_json = output_file.with_suffix(".json")
                if target_json.exists():
                    logger.info(f"Output JSON already exists: {target_json.name}. Skipping JSON merge.")
                else:
                    logger.info(f"Merging {len(part_jsons)} part JSON files -> {target_json.name}")
                    merge_jsons(part_jsons, joined_media_path=output_file, output_json_path=target_json)

def main():
    args = parse_args()

    pkg_paths = []
    dir_pattern = re.compile(r"^\d{6}$")

    if args.package:
        for pkg in args.package:
            resolved_pkg = pkg.resolve()
            if not resolved_pkg.is_dir():
                logger.error(f"Package directory not found: {resolved_pkg}")
                continue
            if not any(resolved_pkg.iterdir()):
                logger.warning(f"Skipping empty package directory: {resolved_pkg}")
                continue
            pkg_paths.append(resolved_pkg)

    elif args.directory:
        for d in args.directory:
            resolved_dir = d.resolve()
            if not resolved_dir.is_dir():
                logger.error(f"Directory not found: {resolved_dir}")
                continue
            if not any(resolved_dir.iterdir()):
                logger.warning(f"Skipping empty directory: {resolved_dir}")
                continue

            if dir_pattern.match(resolved_dir.name):
                pkg_paths.append(resolved_dir)
            else:
                sub_dirs = [
                    sub_d.resolve()
                    for sub_d in resolved_dir.iterdir()
                    if sub_d.is_dir() and any(sub_d.iterdir())
                ]
                pkg_paths.extend(sub_dirs)
    else:
        logger.error("Specify either --package (-p) or --directory (-d).")
        return

    if not pkg_paths:
        logger.warning("No valid packages found.")
        return

    include_sc = not args.no_servicecopies
    logger.info(f"Discovered {len(pkg_paths)} package(s). Service copy processing: {'ENABLED' if include_sc else 'DISABLED'}")

    for pkg_path in pkg_paths:
        process_bag(
            package_path=pkg_path,
            crossfade_sec=args.crossfade,
            min_corr=args.min_corr,
            include_service_copies=include_sc,
            merge_json_enabled=args.merge_json,
            match_gain=args.match_gain,
        )


if __name__ == "__main__":
    main()