"""Audio recording utility for debugging."""
import struct
import os
import threading
from datetime import datetime
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class AudioRecorder:
    """Records audio to WAV files for debugging."""
    
    def __init__(self, output_dir: str = "recordings"):
        """
        Initialize audio recorder.
        
        Args:
            output_dir: Directory to save recordings
        """
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        
        # File handles for recording
        self._input_file: Optional[object] = None  # Audio from ESP32 device
        self._output_file: Optional[object] = None  # Audio from OpenAI
        self._input_bytes = 0
        self._output_bytes = 0
        self._lock = threading.RLock()
        self._input_filename: Optional[str] = None
        self._output_filename: Optional[str] = None
        
    def start_recording(self, client_id: str):
        """
        Start recording audio for a client session.
        
        Args:
            client_id: Unique identifier for this client session
        """
        with self._lock:
            self.stop_recording()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

            # Input audio at the Voice PE websocket boundary: 16 kHz PCM16
            # mono. Recording before resampling preserves exactly what the
            # device uploaded and makes microphone faults distinguishable from
            # resampler/OpenAI faults.
            input_filename = os.path.join(
                self.output_dir,
                f"input_{client_id}_{timestamp}.wav",
            )
            self._input_file = open(input_filename, "w+b", buffering=0)
            self._write_wav_header(
                self._input_file, sample_rate=16000, channels=1, bits_per_sample=16
            )
            self._input_file.flush()
            self._input_bytes = 0
            self._input_filename = input_filename

            # Output audio after the pacer: exactly what is sent to Voice PE.
            output_filename = os.path.join(
                self.output_dir,
                f"output_{client_id}_{timestamp}.wav",
            )
            self._output_file = open(output_filename, "w+b", buffering=0)
            self._write_wav_header(
                self._output_file, sample_rate=24000, channels=1, bits_per_sample=16
            )
            self._output_file.flush()
            self._output_bytes = 0
            self._output_filename = output_filename

            logger.info(
                f"🎙️ Started recording: input={input_filename}, output={output_filename}"
            )
        
    def record_input_audio(self, audio_bytes: bytes):
        """
        Record audio received from ESP32 device.
        
        Args:
            audio_bytes: PCM audio bytes (16-bit, 16kHz, mono)
        """
        with self._lock:
            if not self._input_file or not audio_bytes:
                return
            # Validate audio format: 16-bit = 2 bytes per sample
            if len(audio_bytes) % 2 != 0:
                logger.warning(f"⚠️ Input audio has odd byte count: {len(audio_bytes)}, padding with zero")
                audio_bytes = audio_bytes + b'\x00'  # Pad with one zero byte
            self._input_file.seek(0, os.SEEK_END)
            self._input_file.write(audio_bytes)
            self._input_bytes += len(audio_bytes)
            self._update_wav_header(self._input_file, self._input_bytes)
            
    def record_output_audio(self, audio_bytes: bytes):
        """
        Record audio received from OpenAI.
        
        Args:
            audio_bytes: PCM audio bytes (16-bit, 24kHz, mono)
        """
        with self._lock:
            if not self._output_file or not audio_bytes:
                return
            # Validate audio format: 16-bit = 2 bytes per sample
            if len(audio_bytes) % 2 != 0:
                logger.warning(f"⚠️ Output audio has odd byte count: {len(audio_bytes)}, padding with zero")
                audio_bytes = audio_bytes + b'\x00'  # Pad with one zero byte
            self._output_file.seek(0, os.SEEK_END)
            self._output_file.write(audio_bytes)
            self._output_bytes += len(audio_bytes)
            self._update_wav_header(self._output_file, self._output_bytes)
            
    def stop_recording(self):
        """Stop recording and finalize WAV files."""
        with self._lock:
            if self._input_file:
            # Flush any pending writes before updating header
                self._input_file.flush()
            # Update WAV header with actual data size
            # WAV format: RIFF header (12 bytes) + fmt chunk (24 bytes) + data header (8 bytes) = 44 bytes
            # File size field (position 4) = total_file_size - 8 = (44 + data_size) - 8 = 36 + data_size
                self._update_wav_header(self._input_file, self._input_bytes)
                self._input_file.close()
                self._input_file = None
                logger.info(f"✅ Stopped input recording: {self._input_bytes} bytes")
            
            if self._output_file:
            # Flush any pending writes before updating header
                self._output_file.flush()
            # Update WAV header with actual data size
            # WAV format: RIFF header (12 bytes) + fmt chunk (24 bytes) + data header (8 bytes) = 44 bytes
            # File size field (position 4) = total_file_size - 8 = (44 + data_size) - 8 = 36 + data_size
                self._update_wav_header(self._output_file, self._output_bytes)
                self._output_file.close()
                self._output_file = None
                logger.info(f"✅ Stopped output recording: {self._output_bytes} bytes")

    @staticmethod
    def _update_wav_header(file, data_size: int):
        """Keep an active recording valid and playable after every append."""
        file.seek(4)
        file.write(struct.pack('<I', 36 + data_size))
        file.seek(40)
        file.write(struct.pack('<I', data_size))
        file.seek(0, os.SEEK_END)
        file.flush()
            
    def _write_wav_header(self, file, sample_rate: int, channels: int, bits_per_sample: int):
        """
        Write WAV file header.
        
        Args:
            file: File handle
            sample_rate: Sample rate in Hz
            channels: Number of channels (1=mono, 2=stereo)
            bits_per_sample: Bits per sample (16 or 24)
        """
        byte_rate = sample_rate * channels * (bits_per_sample // 8)
        block_align = channels * (bits_per_sample // 8)
        
        # RIFF header
        file.write(b'RIFF')
        file.write(struct.pack('<I', 0))  # File size (will be updated later)
        file.write(b'WAVE')
        
        # fmt chunk
        file.write(b'fmt ')
        file.write(struct.pack('<I', 16))  # fmt chunk size
        file.write(struct.pack('<H', 1))  # Audio format (1 = PCM)
        file.write(struct.pack('<H', channels))
        file.write(struct.pack('<I', sample_rate))
        file.write(struct.pack('<I', byte_rate))
        file.write(struct.pack('<H', block_align))
        file.write(struct.pack('<H', bits_per_sample))
        
        # data chunk
        file.write(b'data')
        file.write(struct.pack('<I', 0))  # Data size (will be updated later)
