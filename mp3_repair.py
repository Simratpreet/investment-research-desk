"""Repair the duration header of a concatenated MP3 stream.

Some TTS providers (Kokoro via OpenRouter) synthesize long text in segments and
return the encoded MP3 segments simply glued together. Each segment carries its
own Xing header, so the one at the front of the file describes only the *first*
segment. Players that trust it — Safari/WebKit does — report that segment's
length as the whole clip's duration, fire `ended` there, refuse to seek beyond
it, and (being "ended") ignore playbackRate changes. Chrome ignores Xing and
estimates from the bitrate instead, which is merely inaccurate.

`repair_xing_header` walks every frame, then rewrites the leading Xing/Info
fields so they describe the whole stream. Bytes in, bytes out; anything it does
not fully understand is returned untouched, because a clip with a bad duration
still plays and a corrupted one does not.
"""

import struct

_SYNC_MASK = 0xFFE0

_MPEG1, _MPEG2, _MPEG2_5 = 3, 2, 0
_LAYER_III = 1

# Layer III bitrates (kbps) by version group, indexed by the header's 4 bits.
_BITRATES = {
    _MPEG1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0),
    _MPEG2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
}
_SAMPLE_RATES = {
    _MPEG1: (44100, 48000, 32000),
    _MPEG2: (22050, 24000, 16000),
    _MPEG2_5: (11025, 12000, 8000),
}
_SAMPLES_PER_FRAME = {_MPEG1: 1152, _MPEG2: 576, _MPEG2_5: 576}

_XING_FLAG_FRAMES, _XING_FLAG_BYTES, _XING_FLAG_TOC = 1, 2, 4
_TOC_ENTRIES = 100


class _Frame:
    __slots__ = ("offset", "length", "samples", "rate")

    def __init__(self, offset, length, samples, rate):
        self.offset, self.length = offset, length
        self.samples, self.rate = samples, rate


def _id3_end(data: bytes) -> int:
    """Byte offset of the first MPEG frame, skipping any ID3v2 tag."""
    if len(data) < 10 or data[:3] != b"ID3":
        return 0
    # Size is 4 syncsafe bytes (7 bits each) covering the tag body only.
    size = 0
    for byte in data[6:10]:
        size = (size << 7) | (byte & 0x7F)
    return 10 + size


def _parse_frame(data: bytes, pos: int):
    """One frame at `pos`, or None if that isn't a Layer III frame we can size."""
    if pos + 4 > len(data):
        return None
    header = struct.unpack(">I", data[pos:pos + 4])[0]
    if (header >> 16) & _SYNC_MASK != _SYNC_MASK:
        return None
    version = (header >> 19) & 0b11
    layer = (header >> 17) & 0b11
    bitrate_index = (header >> 12) & 0b1111
    rate_index = (header >> 10) & 0b11
    padding = (header >> 9) & 0b1
    if version == 1 or layer != _LAYER_III:      # 1 = reserved version
        return None
    if bitrate_index in (0, 15) or rate_index == 3:   # free-form or reserved
        return None

    rate = _SAMPLE_RATES[version][rate_index]
    bitrate = _BITRATES[_MPEG1 if version == _MPEG1 else _MPEG2][bitrate_index] * 1000
    samples = _SAMPLES_PER_FRAME[version]
    # Layer III frame length: samples/8 bytes of payload at this bitrate.
    length = (samples // 8) * bitrate // rate + padding
    if length < 4:
        return None
    return _Frame(pos, length, samples, rate)


def _iter_frames(data: bytes, start: int):
    pos = start
    while pos < len(data):
        frame = _parse_frame(data, pos)
        if frame is None or pos + frame.length > len(data):
            return
        yield frame
        pos += frame.length


def _tag_position(data: bytes, first: _Frame):
    """Offset of the Xing/Info tag inside the first frame, or None."""
    window = data[first.offset:first.offset + first.length]
    for marker in (b"Xing", b"Info"):
        found = window.find(marker)
        if found != -1:
            return first.offset + found
    return None


def _build_toc(audio: list, total_bytes: int, duration: float, origin: int) -> bytes:
    """Seek table: entry i is the byte position, in 256ths of the file, of the
    point i/100 of the way through the clip."""
    toc = bytearray(_TOC_ENTRIES)
    elapsed, index = 0.0, 0
    for i in range(_TOC_ENTRIES):
        target = duration * i / _TOC_ENTRIES
        while index < len(audio) - 1 and elapsed < target:
            elapsed += audio[index].samples / audio[index].rate
            index += 1
        position = audio[index].offset - origin
        toc[i] = min(255, position * 256 // total_bytes)
    return bytes(toc)


def repair_xing_header(data: bytes) -> bytes:
    """Return `data` with its leading Xing/Info header describing the whole
    stream. Returns the input unchanged when there is no such header, when the
    stream can't be fully parsed, or when the header is already correct."""
    try:
        origin = _id3_end(data)
        frames = list(_iter_frames(data, origin))
        if len(frames) < 2:
            return data
        tag = _tag_position(data, frames[0])
        if tag is None:
            return data

        audio = frames[1:]          # the Xing frame itself carries no audio
        duration = sum(f.samples / f.rate for f in audio)
        if duration <= 0:
            return data
        total_bytes = len(data) - origin
        # Players derive duration as frames × samples-per-frame ÷ sample-rate,
        # both taken from the first frame. Where segments were encoded at
        # different rates that isn't the frame count, so state the count that
        # yields the true duration rather than the literal one.
        head = frames[0]
        frame_count = max(1, round(duration * head.rate / head.samples))

        flags = struct.unpack(">I", data[tag + 4:tag + 8])[0]
        cursor = tag + 8
        patched = bytearray(data)
        if flags & _XING_FLAG_FRAMES:
            patched[cursor:cursor + 4] = struct.pack(">I", frame_count)
            cursor += 4
        if flags & _XING_FLAG_BYTES:
            patched[cursor:cursor + 4] = struct.pack(">I", total_bytes)
            cursor += 4
        if flags & _XING_FLAG_TOC:
            end = cursor + _TOC_ENTRIES
            if end > head.offset + head.length:
                return data          # TOC would run past the frame — leave it alone
            patched[cursor:end] = _build_toc(audio, total_bytes, duration, origin)
        return bytes(patched)
    except (struct.error, IndexError, ValueError, ZeroDivisionError):
        return data


def stream_duration(data: bytes):
    """Playing time in seconds, or None if the stream can't be parsed."""
    try:
        frames = list(_iter_frames(data, _id3_end(data)))
        if not frames:
            return None
        tag = _tag_position(data, frames[0])
        audio = frames[1:] if tag is not None else frames
        return sum(f.samples / f.rate for f in audio) or None
    except (struct.error, IndexError, ValueError, ZeroDivisionError):
        return None
