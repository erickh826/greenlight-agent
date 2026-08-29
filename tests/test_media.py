"""Tests for the storyboard plan, checked before anything is generated.

Media is the only irreversible spend in the pipeline. Every test here is a way
that three perfectly good images could be three images of the wrong film, or of
a film with words baked into the frame -- both of which cost the same as getting
it right and are only visible after the money is gone.

No Imagen, no Cloud TTS, no GCS. The plan is a separate artefact precisely so
this can be checked for free.
"""

from __future__ import annotations

import asyncio
import sys
import wave
from io import BytesIO
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "etl"))

from app.config import SCENE_COUNT  # noqa: E402
from app.contracts import (  # noqa: E402
    ScenePlan, StoryboardPlan, TreatmentProposal)
from app.media import (  # noqa: E402
    AUDIO_MIME_TYPE, IMAGE_MIME_TYPE, DEFAULT_IMAGE_MODEL, DEFAULT_TTS_VOICE,
    GoogleMediaClient, HOUSE_STYLE, SynthesizedAudio, audio_duration_sec,
    compose_image_prompt, estimate_duration_sec, lettering_requests,
    render_storyboard_media, restates_house_style, scene_object_prefix,
    validate_storyboard_plan, wav_duration_sec)

PROPOSAL = TreatmentProposal(
    variant="grounded",
    title="The Unveiling",
    logline="An auditor finds the ledger that explains her father's silence.",
    motif_tags=["hidden_conspiracy", "loss_of_innocence"],
    character_archetypes=["authority_figure", "caregiver"],
    act_structure="classic_three_act",
    rationale="The pairing carries a 5.68 median ROI over 13 films.",
    evidence=[],
)


def scene(i: int, **over) -> ScenePlan:
    base = dict(
        scene_index=i,
        description=f"Beat {i}: she opens another drawer.",
        image_prompt=f"A woman alone in a records room, beat {i}, dust in "
                     "low window light",
        narration=f"Some things are filed away for a reason. Beat {i}.",
    )
    return ScenePlan(**{**base, **over})


def plan(scenes=None, **over) -> StoryboardPlan:
    base = dict(
        proposal_title=PROPOSAL.title,
        variant=PROPOSAL.variant,
        style="Cold municipal interiors, one warm lamp per frame.",
        scenes=scenes if scenes is not None
        else [scene(i) for i in range(SCENE_COUNT)],
    )
    return StoryboardPlan(**{**base, **over})


# --- a good plan passes -----------------------------------------------------

def test_a_faithful_plan_validates():
    assert validate_storyboard_plan(plan(), PROPOSAL) == []


def test_scene_plans_carry_everything_a_scene_asset_needs():
    """Description for the viewer, a prompt for Imagen, a line for TTS."""
    p = plan()
    assert len(p.scenes) == SCENE_COUNT
    for s in p.scenes:
        assert s.description and s.image_prompt and s.narration
        assert estimate_duration_sec(s.narration) > 0


# --- it must still be the film that was approved ----------------------------

def test_a_renamed_treatment_is_rejected():
    errors = validate_storyboard_plan(plan(proposal_title="The Reveal"),
                                      PROPOSAL)
    assert any("approved proposal is" in e for e in errors)


def test_a_switched_variant_is_rejected():
    errors = validate_storyboard_plan(plan(variant="wildcard"), PROPOSAL)
    assert any("approved was" in e for e in errors)


def test_introducing_a_motif_nobody_approved_is_rejected():
    """The person picked a proposal, not a theme the storyboard liked better."""
    drifted = plan(scenes=[
        scene(0), scene(1),
        scene(2, description="She burns the ledger: `revenge` at last."),
    ])
    errors = validate_storyboard_plan(drifted, PROPOSAL)
    assert any("does not carry" in e and "revenge" in e for e in errors)


def test_the_proposals_own_vocabulary_is_fine():
    kept = plan(scenes=[
        scene(0, description="The `hidden_conspiracy` surfaces in a footnote."),
        scene(1), scene(2),
    ])
    assert validate_storyboard_plan(kept, PROPOSAL) == []


def test_ordinary_english_is_not_mistaken_for_a_tag():
    """"Survival" in a sentence is prose, not a claim about the tag set."""
    prose = plan(scenes=[
        scene(0, narration="It was survival, and she knew it."),
        scene(1), scene(2),
    ])
    assert validate_storyboard_plan(prose, PROPOSAL) == []


# --- structure the player depends on ----------------------------------------

def test_wrong_scene_count_is_rejected():
    errors = validate_storyboard_plan(plan(scenes=[scene(0), scene(1)]),
                                      PROPOSAL)
    assert any(f"expected {SCENE_COUNT}" in e for e in errors)


def test_duplicate_scene_indices_are_rejected():
    errors = validate_storyboard_plan(
        plan(scenes=[scene(0), scene(0), scene(2)]), PROPOSAL)
    assert any("exactly once" in e for e in errors)


def test_scenes_out_of_playback_order_are_rejected():
    errors = validate_storyboard_plan(
        plan(scenes=[scene(0), scene(2), scene(1)]), PROPOSAL)
    assert any("out of playback order" in e for e in errors)


def test_an_empty_style_is_rejected():
    errors = validate_storyboard_plan(plan(style="   "), PROPOSAL)
    assert any("read as one pitch" in e for e in errors)


# --- lettering in the frame -------------------------------------------------

def test_a_prompt_asking_for_a_title_card_is_rejected():
    """Generated typography spells nothing and reads as fake instantly."""
    lettered = plan(scenes=[
        scene(0, image_prompt="A title card reading DAY ONE over a dark room"),
        scene(1), scene(2),
    ])
    errors = validate_storyboard_plan(lettered, PROPOSAL)
    assert any("rendered text" in e for e in errors)


def test_quoted_lettering_is_caught():
    assert lettering_requests('a door with a plaque that reads "ARCHIVE"')
    assert lettering_requests('a banner spelling out the words "GO HOME"')


def test_a_negated_mention_is_compliance_not_a_request():
    """The house style is a list of negations, and so are good prompts.

    The first live run flagged all three scenes because the check ran against
    the composed prompt, which carries the house style's own "No text, no
    captions, no logos, no watermarks".
    """
    assert lettering_requests(HOUSE_STYLE) == []
    assert lettering_requests("a quiet street, no captions, no watermark") == []
    assert lettering_requests("an empty wall without a logo") == []
    # ... but a positive request in the same prompt is still caught.
    assert lettering_requests(
        "no watermark, but a title card reading THE END") == ["title card"]


def test_objects_that_merely_could_carry_text_are_not_flagged():
    """A newspaper on a table is a prop, not a request for typography."""
    assert lettering_requests(
        "a newspaper folded on a kitchen table, a road sign in fog, "
        "an open book beside a lamp") == []


# --- prompt composition -----------------------------------------------------

def test_the_subject_leads_and_the_register_trails():
    """Imagen weights the opening; the moment goes first, modifiers after."""
    p = plan()
    composed = compose_image_prompt(p, p.scenes[0])
    assert composed.startswith(p.scenes[0].image_prompt.strip())
    assert composed.endswith(HOUSE_STYLE)
    assert p.style.strip() in composed


def test_every_scene_shares_the_same_register():
    p = plan()
    prompts = [compose_image_prompt(p, s) for s in p.scenes]
    assert all(x.endswith(f"{p.style.strip()} {HOUSE_STYLE}") for x in prompts)


# --- the register must not be said three times ------------------------------

def test_a_style_that_restates_the_house_style_is_rejected():
    """What the first live run actually did.

    The model set style to the house style almost verbatim while the scene
    prompts restated it again, so the composed prompt opened with the same film
    stock vocabulary three times before reaching the subject.
    """
    echoed = plan(style=HOUSE_STYLE)
    errors = validate_storyboard_plan(echoed, PROPOSAL)
    assert any("restates the house style" in e for e in errors)


def test_a_film_specific_style_is_accepted():
    assert not restates_house_style(
        "Rain-soaked municipal Belfast, 1994, sodium streetlight on wet brick.")
    assert validate_storyboard_plan(
        plan(style="Rain-soaked municipal Belfast, 1994, sodium streetlight."),
        PROPOSAL) == []


def test_an_empty_style_is_not_called_a_restatement():
    assert not restates_house_style("   ")


def test_duration_never_collapses_to_nothing():
    """A short line still needs a beat to sit on screen."""
    assert estimate_duration_sec("Go.") >= 3.0
    assert estimate_duration_sec(" ".join(["word"] * 50)) > 15


# --- media generation inputs and outputs -----------------------------------

def wav_bytes(duration_sec: float = 1.0, rate: int = 8000) -> bytes:
    data = BytesIO()
    with wave.open(data, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\0\0" * int(duration_sec * rate))
    return data.getvalue()


class FakeMediaClient:
    def __init__(self):
        self.prompts = []
        self.narrations = []
        self.uploads = []

    async def generate_image(self, prompt: str) -> bytes:
        self.prompts.append(prompt)
        return b"\xff\xd8\xff fake jpeg"

    async def synthesize(self, text: str) -> SynthesizedAudio:
        self.narrations.append(text)
        audio = wav_bytes(4.25)
        return SynthesizedAudio(audio, audio_duration_sec(audio, text))

    async def upload(self, object_name: str, data: bytes,
                     content_type: str) -> str:
        self.uploads.append((object_name, content_type, len(data)))
        return f"https://storage.example/{object_name}"


def test_wav_duration_reads_cloud_tts_linear16_header():
    assert wav_duration_sec(wav_bytes(1.25)) == pytest.approx(1.25)


def test_audio_duration_falls_back_when_audio_is_not_wav():
    assert audio_duration_sec(b"not a wav", "one two three") == \
        estimate_duration_sec("one two three")


def test_scene_object_prefix_is_run_scoped_and_stable():
    assert scene_object_prefix("run-1.bad", scene(2)) == \
        "runs/run-1_bad/scene_2"


def test_render_storyboard_media_returns_three_scene_assets():
    p = plan()
    client = FakeMediaClient()
    progress = []

    assets = asyncio.run(render_storyboard_media(
        "run-1", p, client=client, progress=progress.append))

    assert len(assets) == SCENE_COUNT
    assert [a.scene_index for a in assets] == [0, 1, 2]
    assert [a.description for a in assets] == \
        [s.description for s in p.scenes]
    assert all(a.duration_sec >= 4.25 for a in assets)
    assert all(a.image_url.endswith(f"scene_{i}/image.jpg")
               for i, a in enumerate(assets))
    assert all(a.audio_url.endswith(f"scene_{i}/narration.wav")
               for i, a in enumerate(assets))

    assert client.prompts == [compose_image_prompt(p, s) for s in p.scenes]
    assert client.narrations == [s.narration for s in p.scenes]
    assert [u[:2] for u in client.uploads] == [
        ("runs/run-1/scene_0/image.jpg", IMAGE_MIME_TYPE),
        ("runs/run-1/scene_0/narration.wav", AUDIO_MIME_TYPE),
        ("runs/run-1/scene_1/image.jpg", IMAGE_MIME_TYPE),
        ("runs/run-1/scene_1/narration.wav", AUDIO_MIME_TYPE),
        ("runs/run-1/scene_2/image.jpg", IMAGE_MIME_TYPE),
        ("runs/run-1/scene_2/narration.wav", AUDIO_MIME_TYPE),
    ]
    assert progress[0] == "scene 1/3: image"
    assert progress[-1] == "scene 3/3: upload"


def test_google_media_client_requires_a_bucket(monkeypatch):
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    with pytest.raises(RuntimeError, match="GCS_BUCKET"):
        GoogleMediaClient.from_env()


def test_google_media_client_reads_demo_settings_from_env(monkeypatch):
    monkeypatch.setenv("GCS_BUCKET", "greenlight-demo")
    monkeypatch.setenv("MODEL_IMAGE", "imagen-test")
    monkeypatch.setenv("MODEL_TTS_VOICE", "en-US-Chirp3-HD-Leda")
    monkeypatch.setenv("GCS_SIGNED_URL_TTL_SEC", "900")
    monkeypatch.setenv("GCS_PUBLIC_ASSETS", "true")

    client = GoogleMediaClient.from_env()

    assert client.bucket_name == "greenlight-demo"
    assert client.image_model == "imagen-test"
    assert client.tts_voice == "en-US-Chirp3-HD-Leda"
    assert client.signed_url_ttl_sec == 900
    assert client.public_assets is True


def test_google_media_client_defaults_to_current_google_models(monkeypatch):
    monkeypatch.setenv("GCS_BUCKET", "greenlight-demo")
    monkeypatch.delenv("MODEL_IMAGE", raising=False)
    monkeypatch.delenv("MODEL_TTS_VOICE", raising=False)

    client = GoogleMediaClient.from_env()

    assert client.image_model == DEFAULT_IMAGE_MODEL
    assert client.tts_voice == DEFAULT_TTS_VOICE


class FakeBlob:
    public_url = "https://storage.example/public/object"

    def __init__(self):
        self.uploaded = None
        self.signed_kwargs = None

    def upload_from_string(self, data, content_type):
        self.uploaded = (data, content_type)

    def generate_signed_url(self, **kwargs):
        self.signed_kwargs = kwargs
        return "https://storage.example/signed/object"


class FakeBucket:
    def __init__(self, blob):
        self._blob = blob

    def blob(self, object_name):
        self.object_name = object_name
        return self._blob


class FakeStorage:
    def __init__(self, blob):
        self.bucket_obj = FakeBucket(blob)

    def bucket(self, bucket_name):
        self.bucket_name = bucket_name
        return self.bucket_obj


def test_upload_returns_signed_url_by_default():
    blob = FakeBlob()
    storage = FakeStorage(blob)
    client = GoogleMediaClient(
        bucket_name="greenlight-demo",
        image_model=DEFAULT_IMAGE_MODEL,
        tts_voice=DEFAULT_TTS_VOICE,
        signed_url_ttl_sec=900,
    )
    client._storage_client = storage

    url = client._upload_sync("runs/r/scene_0/image.jpg", b"image",
                              IMAGE_MIME_TYPE)

    assert url == "https://storage.example/signed/object"
    assert storage.bucket_name == "greenlight-demo"
    assert storage.bucket_obj.object_name == "runs/r/scene_0/image.jpg"
    assert blob.uploaded == (b"image", IMAGE_MIME_TYPE)
    assert blob.signed_kwargs["version"] == "v4"
    assert blob.signed_kwargs["method"] == "GET"
    assert blob.signed_kwargs["response_type"] == IMAGE_MIME_TYPE


def test_upload_can_use_public_demo_bucket_without_signing():
    blob = FakeBlob()
    storage = FakeStorage(blob)
    client = GoogleMediaClient(
        bucket_name="greenlight-demo",
        image_model=DEFAULT_IMAGE_MODEL,
        tts_voice=DEFAULT_TTS_VOICE,
        signed_url_ttl_sec=900,
        public_assets=True,
    )
    client._storage_client = storage

    url = client._upload_sync("runs/r/scene_0/narration.wav", b"audio",
                              AUDIO_MIME_TYPE)

    assert url == blob.public_url
    assert blob.uploaded == (b"audio", AUDIO_MIME_TYPE)
    assert blob.signed_kwargs is None
