"""Tests for sovereign_claw.media_pipeline."""

from __future__ import annotations

import os
import time

import pytest

from sovereign_claw.media_pipeline import (
    MediaArtifact,
    MediaMetadata,
    MediaPipeline,
    MediaSizeCap,
    MediaStatus,
    MediaType,
    TranscriptionResult,
    TranscriptionStatus,
    compute_file_hash,
    detect_media_type,
)


# ── MediaType detection ─────────────────────────────────────────────────────


class TestDetectMediaType:
    def test_image_extensions(self) -> None:
        for ext in ("png", "jpg", "jpeg", "gif", "webp", "svg", "bmp"):
            assert detect_media_type(f"photo.{ext}") == MediaType.IMAGE

    def test_audio_extensions(self) -> None:
        for ext in ("mp3", "wav", "ogg", "flac", "aac", "m4a"):
            assert detect_media_type(f"song.{ext}") == MediaType.AUDIO

    def test_video_extensions(self) -> None:
        for ext in ("mp4", "avi", "mov", "mkv", "webm"):
            assert detect_media_type(f"clip.{ext}") == MediaType.VIDEO

    def test_document_extensions(self) -> None:
        for ext in ("pdf", "txt", "md", "csv", "json"):
            assert detect_media_type(f"file.{ext}") == MediaType.DOCUMENT

    def test_unknown_extension(self) -> None:
        assert detect_media_type("file.xyz") == MediaType.UNKNOWN

    def test_no_extension(self) -> None:
        assert detect_media_type("README") == MediaType.UNKNOWN

    def test_case_insensitive(self) -> None:
        assert detect_media_type("photo.PNG") == MediaType.IMAGE
        assert detect_media_type("song.MP3") == MediaType.AUDIO


# ── compute_file_hash ────────────────────────────────────────────────────────


class TestComputeFileHash:
    def test_hash_deterministic(self) -> None:
        data = b"hello world"
        h1 = compute_file_hash(data)
        h2 = compute_file_hash(data)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_different_content_different_hash(self) -> None:
        assert compute_file_hash(b"aaa") != compute_file_hash(b"bbb")

    def test_empty_data(self) -> None:
        h = compute_file_hash(b"")
        assert len(h) == 64


# ── MediaSizeCap ─────────────────────────────────────────────────────────────


class TestMediaSizeCap:
    def test_default_caps(self) -> None:
        cap = MediaSizeCap()
        assert cap.max_bytes > 0
        assert cap.max_width > 0

    def test_validate_size_within_limit(self) -> None:
        cap = MediaSizeCap(max_bytes=100)
        ok, err = cap.validate_size(50)
        assert ok
        assert err == ""

    def test_validate_size_exceeds_limit(self) -> None:
        cap = MediaSizeCap(max_bytes=100)
        ok, err = cap.validate_size(101)
        assert not ok
        assert "exceeds" in err

    def test_validate_extension_allowed(self) -> None:
        cap = MediaSizeCap()
        ok, err = cap.validate_extension("photo.png")
        assert ok

    def test_validate_extension_blocked(self) -> None:
        cap = MediaSizeCap()
        ok, err = cap.validate_extension("virus.exe")
        assert not ok
        assert "not allowed" in err


# ── MediaMetadata ────────────────────────────────────────────────────────────


class TestMediaMetadata:
    def test_defaults(self) -> None:
        meta = MediaMetadata()
        assert meta.width == 0
        assert meta.height == 0
        assert meta.duration_seconds == 0.0

    def test_to_dict(self) -> None:
        meta = MediaMetadata(width=1920, height=1080, codec="h264")
        d = meta.to_dict()
        assert d["width"] == 1920
        assert d["codec"] == "h264"

    def test_to_dict_minimal(self) -> None:
        meta = MediaMetadata(filename="test.txt", size_bytes=42)
        d = meta.to_dict()
        assert d["filename"] == "test.txt"
        assert d["size_bytes"] == 42


# ── TranscriptionResult ─────────────────────────────────────────────────────


class TestTranscriptionResult:
    def test_defaults(self) -> None:
        tr = TranscriptionResult(text="hello")
        assert tr.status == TranscriptionStatus.PENDING
        assert tr.text == "hello"

    def test_completed_status(self) -> None:
        tr = TranscriptionResult(text="hello", status=TranscriptionStatus.COMPLETED)
        assert tr.status == TranscriptionStatus.COMPLETED

    def test_to_dict(self) -> None:
        tr = TranscriptionResult(text="test", provider="whisper", confidence=0.95)
        d = tr.to_dict()
        assert d["provider"] == "whisper"
        assert d["confidence"] == 0.95


# ── MediaArtifact ────────────────────────────────────────────────────────────


class TestMediaArtifact:
    def test_creation(self) -> None:
        art = MediaArtifact(
            filename="test.png",
            media_type=MediaType.IMAGE,
        )
        assert art.artifact_id.startswith("media_")
        assert art.status == MediaStatus.PENDING
        assert art.filename == "test.png"

    def test_expiration(self) -> None:
        now = time.time()
        art = MediaArtifact(
            filename="test.mp3",
            media_type=MediaType.AUDIO,
            created_at=now - 10,
            expires_at=now - 5,
        )
        assert art.is_expired

    def test_not_expired(self) -> None:
        now = time.time()
        art = MediaArtifact(
            filename="test.mp4",
            media_type=MediaType.VIDEO,
            created_at=now,
            expires_at=now + 3600,
        )
        assert not art.is_expired

    def test_default_ttl(self) -> None:
        art = MediaArtifact(filename="test.pdf", media_type=MediaType.DOCUMENT)
        # Default TTL is 1 hour
        assert art.expires_at > art.created_at

    def test_to_dict(self) -> None:
        art = MediaArtifact(
            filename="test.png",
            media_type=MediaType.IMAGE,
        )
        d = art.to_dict()
        assert d["filename"] == "test.png"
        assert d["media_type"] == "image"
        assert d["status"] == "pending"

    def test_metadata_access(self) -> None:
        art = MediaArtifact(
            filename="test.png",
            media_type=MediaType.IMAGE,
            metadata=MediaMetadata(size_bytes=2048),
        )
        assert art.metadata.size_bytes == 2048


# ── MediaPipeline ────────────────────────────────────────────────────────────


class TestMediaPipeline:
    def test_create_pipeline(self) -> None:
        pipeline = MediaPipeline()
        assert pipeline.stats()["active_artifacts"] == 0

    def test_ingest_basic(self) -> None:
        pipeline = MediaPipeline()
        art = pipeline.ingest(data=b"fake image data", filename="test.png")
        assert art.filename == "test.png"
        assert art.media_type == MediaType.IMAGE
        assert art.status == MediaStatus.PENDING
        assert art.metadata.size_bytes == len(b"fake image data")

    def test_ingest_with_tags(self) -> None:
        pipeline = MediaPipeline()
        art = pipeline.ingest(
            data=b"audio data",
            filename="song.mp3",
            tags=["music", "demo"],
        )
        assert "music" in art.tags
        assert "demo" in art.tags

    def test_ingest_with_ttl(self) -> None:
        pipeline = MediaPipeline()
        art = pipeline.ingest(
            data=b"data",
            filename="temp.png",
            ttl=60,
        )
        # expires_at should be ~60 seconds from now
        assert art.expires_at > art.created_at
        assert art.expires_at <= art.created_at + 61

    def test_ingest_size_exceeded(self) -> None:
        cap = MediaSizeCap(max_bytes=10)
        pipeline = MediaPipeline(size_caps=cap)
        art = pipeline.ingest(data=b"x" * 20, filename="big.png")
        assert art.status == MediaStatus.REJECTED
        assert "exceeds" in art.error

    def test_ingest_blocked_extension(self) -> None:
        pipeline = MediaPipeline()
        art = pipeline.ingest(data=b"data", filename="virus.exe")
        assert art.status == MediaStatus.REJECTED
        assert "not allowed" in art.error

    def test_get_artifact(self) -> None:
        pipeline = MediaPipeline()
        art = pipeline.ingest(data=b"data", filename="test.jpg")
        found = pipeline.get_artifact(art.artifact_id)
        assert found is not None
        assert found.artifact_id == art.artifact_id

    def test_get_artifact_not_found(self) -> None:
        pipeline = MediaPipeline()
        assert pipeline.get_artifact("nonexistent") is None

    def test_process_artifact(self) -> None:
        pipeline = MediaPipeline()
        art = pipeline.ingest(data=b"image content", filename="photo.jpg")
        processed = pipeline.process(art.artifact_id)
        assert processed.status == MediaStatus.COMPLETED

    def test_process_not_found(self) -> None:
        pipeline = MediaPipeline()
        with pytest.raises(ValueError):
            pipeline.process("nonexistent")

    def test_register_transcription_provider(self) -> None:
        pipeline = MediaPipeline()

        def mock_transcribe(data: bytes, filename: str) -> TranscriptionResult:
            return TranscriptionResult(
                text="hello world",
                status=TranscriptionStatus.COMPLETED,
                provider="mock",
            )

        pipeline.register_transcription_provider("mock", mock_transcribe)
        art = pipeline.ingest(data=b"audio", filename="speech.mp3")
        processed = pipeline.process(art.artifact_id, transcription_provider="mock")
        assert processed.transcription is not None
        assert processed.transcription.text == "hello world"

    def test_cleanup_expired(self) -> None:
        pipeline = MediaPipeline()
        art = pipeline.ingest(data=b"data", filename="temp.png", ttl=1)
        # Force expiration by backdating created_at and expires_at
        a = pipeline._artifacts[art.artifact_id]
        a.created_at = time.time() - 10
        a.expires_at = time.time() - 5
        removed = pipeline.cleanup_expired()
        assert removed >= 1
        assert pipeline.get_artifact(art.artifact_id) is None

    def test_stats(self) -> None:
        pipeline = MediaPipeline()
        pipeline.ingest(data=b"a", filename="a.png")
        pipeline.ingest(data=b"b", filename="b.jpg")
        stats = pipeline.stats()
        assert stats["active_artifacts"] == 2
        assert stats["total_ingested"] == 2

    def test_stats_by_type(self) -> None:
        pipeline = MediaPipeline()
        pipeline.ingest(data=b"a", filename="a.png")
        pipeline.ingest(data=b"b", filename="b.mp3")
        stats = pipeline.stats()
        assert "image" in stats["by_type"]
        assert "audio" in stats["by_type"]

    def test_processing_hook(self) -> None:
        pipeline = MediaPipeline()
        hook_called: list[str] = []

        def my_hook(artifact: MediaArtifact) -> None:
            hook_called.append(artifact.artifact_id)

        pipeline.add_processing_hook(my_hook)
        art = pipeline.ingest(data=b"data", filename="test.png")
        pipeline.process(art.artifact_id)
        assert len(hook_called) == 1

    def test_delete_artifact(self) -> None:
        pipeline = MediaPipeline()
        art = pipeline.ingest(data=b"data", filename="test.png")
        assert pipeline.delete_artifact(art.artifact_id)
        assert pipeline.get_artifact(art.artifact_id) is None

    def test_delete_nonexistent(self) -> None:
        pipeline = MediaPipeline()
        assert not pipeline.delete_artifact("nope")

    def test_list_artifacts(self) -> None:
        pipeline = MediaPipeline()
        pipeline.ingest(data=b"a", filename="a.png")
        pipeline.ingest(data=b"b", filename="b.mp3")
        all_arts = pipeline.list_artifacts()
        assert len(all_arts) == 2

    def test_list_artifacts_by_type(self) -> None:
        pipeline = MediaPipeline()
        pipeline.ingest(data=b"a", filename="a.png")
        pipeline.ingest(data=b"b", filename="b.mp3")
        images = pipeline.list_artifacts(media_type=MediaType.IMAGE)
        assert len(images) == 1
        assert images[0].media_type == MediaType.IMAGE

    def test_list_artifacts_by_status(self) -> None:
        pipeline = MediaPipeline()
        pipeline.ingest(data=b"a", filename="a.png")
        pending = pipeline.list_artifacts(status=MediaStatus.PENDING)
        assert len(pending) == 1

    def test_list_artifacts_by_tag(self) -> None:
        pipeline = MediaPipeline()
        pipeline.ingest(data=b"a", filename="a.png", tags=["important"])
        pipeline.ingest(data=b"b", filename="b.png", tags=["other"])
        tagged = pipeline.list_artifacts(tag="important")
        assert len(tagged) == 1

    def test_process_rejected_artifact(self) -> None:
        cap = MediaSizeCap(max_bytes=5)
        pipeline = MediaPipeline(size_caps=cap)
        art = pipeline.ingest(data=b"toolarge", filename="big.png")
        assert art.status == MediaStatus.REJECTED
        processed = pipeline.process(art.artifact_id)
        assert processed.status == MediaStatus.REJECTED

    def test_metadata_sha256(self) -> None:
        pipeline = MediaPipeline()
        art = pipeline.ingest(data=b"test data", filename="test.txt")
        assert len(art.metadata.sha256) == 64

    def test_temp_file_created(self) -> None:
        pipeline = MediaPipeline()
        art = pipeline.ingest(data=b"test data", filename="test.png")
        assert art.temp_path != ""
        assert os.path.exists(art.temp_path)
        # Cleanup
        pipeline.delete_artifact(art.artifact_id)

    def test_temp_file_cleaned_on_delete(self) -> None:
        pipeline = MediaPipeline()
        art = pipeline.ingest(data=b"test data", filename="test.png")
        temp_path = art.temp_path
        pipeline.delete_artifact(art.artifact_id)
        assert not os.path.exists(temp_path)
