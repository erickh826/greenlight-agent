"""Storyboard plans, checked before anything is generated from them.

Media is the one irreversible, billable step in this pipeline. Everything up to
the approval gate can be re-run for the cost of a few ClickHouse queries; three
Imagen frames and three Cloud TTS clips cannot be un-spent. So the plan is
validated against the approved proposal first, for nothing, and what that
catches is not an ugly frame -- it is three good frames of the wrong film.

Generation itself (Imagen, Cloud TTS, GCS) lands in Task 2. This module holds
the parts that can be checked without spending anything, and they are checkable
precisely because the plan is a separate artefact from the assets.
"""

from __future__ import annotations

import asyncio
import os
import re
import wave
from dataclasses import dataclass
from datetime import timedelta
from io import BytesIO
from typing import Callable, Protocol

from app.config import SCENE_COUNT
from app.contracts import (SceneAsset, ScenePlan, StoryboardPlan,
                           TreatmentProposal)
from app.guardrails import unsupported_terms

# Hard-coded rather than model-chosen, and the same for every run. Three images
# generated from three independent prompts look like three films; a shared
# register is what makes them read as one pitch. It also keeps the demo's look
# stable across runs, so a judge comparing two runs sees the ideas differ rather
# than the art direction.
HOUSE_STYLE = (
    "Cinematic 35mm film still, anamorphic widescreen, shallow depth of field, "
    "naturalistic motivated lighting with deep shadows, muted desaturated "
    "palette with one warm accent, fine film grain. No text, no captions, no "
    "logos, no watermarks."
)

# Phrases that ask a diffusion model for lettering. Generated text is the single
# most reliable way to make an image look fake -- it comes out as convincing
# typography spelling nothing -- and the instruction not to is worth a check,
# because it is easy to write "a title card reading DAY ONE" without noticing
# that is a request for letters.
#
# Kept narrow on purpose. "sign", "book" and "newspaper" are legitimate objects
# in a frame; only an explicit request for rendered text is a finding.
LETTERING_PATTERNS = (
    r"\btitle card\b", r"\bcaption(s|ed)?\b", r"\bsubtitle", r"\bwatermark",
    r"\blogo\b", r"\btext overlay\b", r"\boverlaid text\b",
    r"\bthe words?\s+[\"']", r"\breading\s+[\"']", r"\bthat reads?\s+[\"']",
    r"\bspell(s|ing)?\s+out\b",
)

# Cloud TTS Chirp 3 HD lands near 150 words per minute in a narration register.
# Used only as a fallback: Task 2 reads the real length off the synthesised
# audio, because a scene whose image changes before its line finishes is the
# kind of thing nobody notices in code and everybody notices on stage.
WORDS_PER_SECOND = 2.5
MIN_SCENE_SEC = 3.0
IMAGE_MIME_TYPE = "image/jpeg"
AUDIO_MIME_TYPE = "audio/wav"
IMAGE_ASPECT_RATIO = "16:9"
DEFAULT_IMAGE_MODEL = "imagen-4.0-generate-001"
DEFAULT_TTS_VOICE = "en-US-Chirp3-HD-Charon"
DEFAULT_SIGNED_URL_TTL_SEC = 6 * 60 * 60


def compose_image_prompt(plan: StoryboardPlan, scene: ScenePlan) -> str:
    """One frame's prompt: the moment, then this film's look, then the house.

    Subject first. Imagen reads natural language and weights the opening most,
    so leading with the register put four clauses of film-stock vocabulary in
    front of the thing the frame is actually of. The shared style trails it,
    which is where a modifier belongs and still guarantees all three frames
    carry the same one.
    """
    return (f"{scene.image_prompt.strip()} {plan.style.strip()} "
            f"{HOUSE_STYLE}")


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 3}


HOUSE_STYLE_WORDS = _content_words(HOUSE_STYLE)
RESTATEMENT_RATIO = 0.7


def restates_house_style(style: str) -> bool:
    """Whether a plan's style is the house style said again.

    On the first live run the model set style to the house style almost
    verbatim, and the scene prompts restated it a third time -- so the composed
    prompt opened with the same register three times before reaching the
    subject. The style field is for what is specific to this film; a
    restatement is a wasted field, and worth a retry rather than a shrug.
    """
    words = _content_words(style)
    if not words:
        return False
    return len(words & HOUSE_STYLE_WORDS) / len(words) >= RESTATEMENT_RATIO


def estimate_duration_sec(narration: str) -> float:
    """Fallback scene length from the narration, when no audio exists yet."""
    words = len(narration.split())
    return max(MIN_SCENE_SEC, words / WORDS_PER_SECOND)


def wav_duration_sec(audio: bytes) -> float:
    """Duration from a Cloud TTS LINEAR16 response.

    Cloud TTS wraps online LINEAR16 output in a WAV header, which lets the demo
    use the browser's native audio element and still know how long to hold each
    Ken Burns beat.
    """
    with wave.open(BytesIO(audio), "rb") as reader:
        frames = reader.getnframes()
        rate = reader.getframerate()
    if rate <= 0:
        raise ValueError("WAV sample rate is not positive")
    return frames / rate


def audio_duration_sec(audio: bytes, narration: str) -> float:
    """Prefer the real WAV length; fall back to the narration estimate."""
    try:
        return max(MIN_SCENE_SEC, wav_duration_sec(audio))
    except (EOFError, ValueError, wave.Error):
        return estimate_duration_sec(narration)


@dataclass(frozen=True)
class SynthesizedAudio:
    data: bytes
    duration_sec: float


class MediaClient(Protocol):
    async def generate_image(self, prompt: str) -> bytes: ...

    async def synthesize(self, text: str) -> SynthesizedAudio: ...

    async def upload(self, object_name: str, data: bytes,
                     content_type: str) -> str: ...


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return int(value)


def _language_code(voice_name: str) -> str:
    parts = voice_name.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else "en-US"


class GoogleMediaClient:
    """Google-only media adapter: Imagen, Cloud TTS and Cloud Storage.

    Imports stay inside the methods so API startup, /health and unit tests do
    not depend on the media SDKs being importable. The first real call is behind
    approval, which is exactly where this spend belongs.
    """

    def __init__(self, *, bucket_name: str, image_model: str,
                 tts_voice: str, signed_url_ttl_sec: int,
                 public_assets: bool = False) -> None:
        self.bucket_name = bucket_name
        self.image_model = image_model
        self.tts_voice = tts_voice
        self.signed_url_ttl_sec = signed_url_ttl_sec
        self.public_assets = public_assets
        self._genai_client = None
        self._tts_client = None
        self._storage_client = None

    @classmethod
    def from_env(cls) -> "GoogleMediaClient":
        bucket = os.environ.get("GCS_BUCKET", "").strip()
        if not bucket:
            raise RuntimeError("GCS_BUCKET is required for media generation")
        return cls(
            bucket_name=bucket,
            image_model=os.environ.get("MODEL_IMAGE")
            or DEFAULT_IMAGE_MODEL,
            tts_voice=os.environ.get("MODEL_TTS_VOICE")
            or DEFAULT_TTS_VOICE,
            signed_url_ttl_sec=_env_int("GCS_SIGNED_URL_TTL_SEC",
                                        DEFAULT_SIGNED_URL_TTL_SEC),
            public_assets=_env_bool("GCS_PUBLIC_ASSETS"),
        )

    async def generate_image(self, prompt: str) -> bytes:
        return await asyncio.to_thread(self._generate_image_sync, prompt)

    def _generate_image_sync(self, prompt: str) -> bytes:
        from google import genai
        from google.genai import types

        if self._genai_client is None:
            self._genai_client = genai.Client()
        response = self._genai_client.models.generate_images(
            model=self.image_model,
            prompt=prompt,
            config=types.GenerateImagesConfig(
                number_of_images=1,
                aspect_ratio=IMAGE_ASPECT_RATIO,
                include_rai_reason=True,
                output_mime_type=IMAGE_MIME_TYPE,
            ),
        )
        images = getattr(response, "generated_images", None) or []
        if not images:
            reason = getattr(response, "rai_filtered_reason", None) or \
                "no generated_images returned"
            raise RuntimeError(f"Imagen returned no image: {reason}")
        data = getattr(images[0].image, "image_bytes", None)
        if not data:
            raise RuntimeError("Imagen response did not include image bytes")
        return bytes(data)

    async def synthesize(self, text: str) -> SynthesizedAudio:
        return await asyncio.to_thread(self._synthesize_sync, text)

    def _synthesize_sync(self, text: str) -> SynthesizedAudio:
        from google.cloud import texttospeech

        if self._tts_client is None:
            self._tts_client = texttospeech.TextToSpeechClient()
        response = self._tts_client.synthesize_speech(
            input=texttospeech.SynthesisInput(text=text),
            voice=texttospeech.VoiceSelectionParams(
                language_code=_language_code(self.tts_voice),
                name=self.tts_voice,
            ),
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.LINEAR16,
                sample_rate_hertz=24000,
            ),
        )
        audio = bytes(response.audio_content)
        return SynthesizedAudio(audio, audio_duration_sec(audio, text))

    async def upload(self, object_name: str, data: bytes,
                     content_type: str) -> str:
        return await asyncio.to_thread(
            self._upload_sync, object_name, data, content_type)

    def _upload_sync(self, object_name: str, data: bytes,
                     content_type: str) -> str:
        if self._storage_client is None:
            from google.cloud import storage

            project = os.environ.get("GOOGLE_CLOUD_PROJECT") or None
            self._storage_client = storage.Client(project=project)
        blob = self._storage_client.bucket(self.bucket_name).blob(object_name)
        blob.cache_control = "public, max-age=21600"
        blob.upload_from_string(data, content_type=content_type)

        if self.public_assets:
            return blob.public_url
        expiration = timedelta(seconds=self.signed_url_ttl_sec)
        try:
            return self._signed_url(blob, content_type, expiration)
        except Exception:
            try:
                return self._signed_url_with_access_token(
                    blob, content_type, expiration)
            except Exception as second_error:
                raise RuntimeError(
                    "uploaded media but could not create a signed URL; grant "
                    "iam.serviceAccounts.signBlob to the runtime service "
                    "account, enable the IAM Service Account Credentials API, "
                    "or set GCS_PUBLIC_ASSETS=true for a public demo bucket"
                ) from second_error

    def _signed_url(self, blob, content_type: str,
                    expiration: timedelta) -> str:
        return blob.generate_signed_url(
            version="v4",
            expiration=expiration,
            method="GET",
            response_type=content_type,
        )

    def _signed_url_with_access_token(self, blob, content_type: str,
                                      expiration: timedelta) -> str:
        import google.auth
        from google.auth.transport.requests import Request

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        credentials.refresh(Request())
        service_account_email = getattr(
            credentials, "service_account_email", None)
        if not service_account_email or not credentials.token:
            raise RuntimeError(
                "default credentials expose no service_account_email/token")
        try:
            return blob.generate_signed_url(
                version="v4",
                expiration=expiration,
                method="GET",
                response_type=content_type,
                service_account_email=service_account_email,
                access_token=credentials.token,
            )
        except Exception as exc:
            raise RuntimeError("IAM signed URL fallback failed") from exc


def scene_object_prefix(run_id: str, scene: ScenePlan) -> str:
    safe_run = re.sub(r"[^A-Za-z0-9_-]", "_", run_id)
    return f"runs/{safe_run}/scene_{scene.scene_index}"


async def render_storyboard_media(
    run_id: str,
    plan: StoryboardPlan,
    *,
    client: MediaClient | None = None,
    progress: Callable[[str], None] | None = None,
) -> list[SceneAsset]:
    """Generate and upload the three approved scene assets.

    Sequential on purpose: this is the expensive section of the demo, and doing
    one image plus one narration at a time is predictable under quota.
    """
    client = client or GoogleMediaClient.from_env()
    assets: list[SceneAsset] = []
    for scene in plan.scenes:
        scene_no = scene.scene_index + 1
        progress and progress(f"scene {scene_no}/{len(plan.scenes)}: image")
        image = await client.generate_image(compose_image_prompt(plan, scene))

        progress and progress(f"scene {scene_no}/{len(plan.scenes)}: audio")
        audio = await client.synthesize(scene.narration)

        prefix = scene_object_prefix(run_id, scene)
        progress and progress(f"scene {scene_no}/{len(plan.scenes)}: upload")
        image_url = await client.upload(f"{prefix}/image.jpg", image,
                                        IMAGE_MIME_TYPE)
        audio_url = await client.upload(f"{prefix}/narration.wav", audio.data,
                                        AUDIO_MIME_TYPE)
        assets.append(SceneAsset(
            scene_index=scene.scene_index,
            description=scene.description,
            image_url=image_url,
            audio_url=audio_url,
            duration_sec=audio.duration_sec,
        ))
    return assets


# "no captions", "without a watermark" -- a negated mention is compliance, not a
# request. The house style is itself written as a list of negations, so a check
# blind to this flags our own instruction; it did, on the first live run, for
# all three scenes at once.
NEGATION = re.compile(r"(?:\bno\b|\bnot\b|\bwithout\b|\bavoid(?:ing)?\b|"
                      r"\bfree of\b|\bnever\b)[\s\w,]{0,24}$")

# A negation only covers its own clause. Without this, "no watermark, but a
# title card reading THE END" reads as negated all the way to the end, because
# the "no" is still inside the lookback window.
CLAUSE_BREAK = re.compile(r"[.;:]|\bbut\b|\bhowever\b|\byet\b|\bthough\b")


def _is_negated(prefix: str) -> bool:
    """Whether the clause immediately before a match negates it."""
    return bool(NEGATION.search(CLAUSE_BREAK.split(prefix)[-1]))


def lettering_requests(text: str) -> list[str]:
    """Phrases in an image prompt that ask for rendered text.

    Negated mentions are ignored. What is left is a positive request for
    letters, which is worth catching because generated typography spells
    nothing and gives the image away instantly.
    """
    low = text.lower()
    found = set()
    for pattern in LETTERING_PATTERNS:
        for m in re.finditer(pattern, low):
            if _is_negated(low[:m.start()]):
                continue
            found.add(m.group(0).strip())
    return sorted(found)


def validate_storyboard_plan(plan: StoryboardPlan,
                             proposal: TreatmentProposal) -> list[str]:
    """Errors that make a plan unsafe to spend money on. Empty means go.

    The checks fall into two kinds. Structural ones -- the count, the indices --
    are about whether the player can show it. The rest are about whether it is
    still the film that was approved: a storyboard that renames the treatment,
    switches variant or introduces a motif nobody chose has stopped being a
    pitch for the thing the person picked.
    """
    errors: list[str] = []

    if plan.proposal_title.strip() != proposal.title.strip():
        errors.append(
            f"plan is titled {plan.proposal_title!r} but the approved proposal "
            f"is {proposal.title!r}")
    if plan.variant != proposal.variant:
        errors.append(
            f"plan says variant {plan.variant!r}, approved was "
            f"{proposal.variant!r}")
    if not plan.style.strip():
        errors.append("plan has no shared style; the three frames would not "
                      "read as one pitch")
    elif restates_house_style(plan.style):
        errors.append(
            "plan style restates the house style instead of adding this "
            "film's own look; say what is specific to this treatment -- "
            "its locations, palette, era, weather")

    if len(plan.scenes) != SCENE_COUNT:
        errors.append(f"plan has {len(plan.scenes)} scenes, expected "
                      f"{SCENE_COUNT}")
    indices = [s.scene_index for s in plan.scenes]
    if sorted(indices) != list(range(len(plan.scenes))):
        errors.append(f"scene_index values {indices} are not 0.."
                      f"{len(plan.scenes) - 1} exactly once")
    elif indices != sorted(indices):
        errors.append(f"scenes are out of playback order: {indices}")

    for scene in plan.scenes:
        prefix = f"scene[{scene.scene_index}]"
        if not scene.description.strip():
            errors.append(f"{prefix} has no description")
        if not scene.image_prompt.strip():
            errors.append(f"{prefix} has no image_prompt")
        if not scene.narration.strip():
            errors.append(f"{prefix} has no narration")

        lettering = lettering_requests(scene.image_prompt)
        if lettering:
            errors.append(
                f"{prefix} image_prompt asks for rendered text "
                f"({', '.join(lettering)}); generated lettering spells nothing "
                "and reads as fake")

    # The proposal's own tags are what this pitch is selling. A storyboard that
    # brings in a motif the proposal does not carry is describing a different
    # film -- and the terms are closed vocabulary, so this is exact rather than
    # a judgement about theme.
    approved_terms = " ".join(
        [*(m.value for m in proposal.motif_tags),
         *(a.value for a in proposal.character_archetypes),
         proposal.act_structure.value])
    plan_text = "\n".join(
        [plan.style]
        + [f"{s.description}\n{s.image_prompt}\n{s.narration}"
           for s in plan.scenes])
    invented = unsupported_terms(plan_text, approved_terms)
    if invented:
        errors.append(
            "storyboard introduces vocabulary the approved proposal does not "
            "carry: " + ", ".join(invented))

    return errors


__all__ = [
    "AUDIO_MIME_TYPE", "DEFAULT_IMAGE_MODEL", "DEFAULT_SIGNED_URL_TTL_SEC",
    "DEFAULT_TTS_VOICE", "GoogleMediaClient", "IMAGE_ASPECT_RATIO",
    "IMAGE_MIME_TYPE", "LETTERING_PATTERNS", "MIN_SCENE_SEC", "MediaClient",
    "SynthesizedAudio", "WORDS_PER_SECOND", "audio_duration_sec",
    "compose_image_prompt", "estimate_duration_sec", "lettering_requests",
    "render_storyboard_media", "restates_house_style", "scene_object_prefix",
    "validate_storyboard_plan", "wav_duration_sec",
]
