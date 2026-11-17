from typing import Literal

from pydantic import BaseModel, Field, model_validator

from . import config as C

Transition = Literal["none", "flash_cut", "fade", "cross_dissolve", "whip_pan", "shake"]
Purpose = Literal["hook", "plot", "acting", "cinematography", "music", "theme", "verdict", "cta"]


class Cue(BaseModel):
    index: int
    start: float = Field(ge=0)
    end: float
    text: str


class Scene(BaseModel):
    start: float
    end: float
    # Điền bởi bước scene tagging (Claude vision / CLIP). Tuỳ chọn.
    shot_type: str | None = None      # close_up | medium | wide | ...
    description: str | None = None
    spoiler: bool = False
    camera_motion: str | None = None


class Footage(BaseModel):
    file: str
    duration: float
    scenes: list[Scene]
    description: str = ""


class PlanContext(BaseModel):
    movie_title: str
    genre: str = ""
    language: str = "vi"
    target_duration_seconds: int = 480
    allow_spoilers: bool = False
    style: str = ""
    voice_duration: float
    srt_cues: list[Cue]
    available_footage: list[Footage]
    available_filters: list[str]


class Visual(BaseModel):
    footage: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    zoom_start: float = Field(1.0, ge=C.ZOOM_RANGE[0], le=C.ZOOM_RANGE[1])
    zoom_end: float = Field(1.0, ge=C.ZOOM_RANGE[0], le=C.ZOOM_RANGE[1])
    speed: float = Field(1.0, ge=C.SPEED_RANGE[0], le=C.SPEED_RANGE[1])
    brightness: float = Field(1.0, ge=C.BRIGHTNESS_RANGE[0], le=C.BRIGHTNESS_RANGE[1])
    contrast: float = Field(1.0, ge=C.CONTRAST_RANGE[0], le=C.CONTRAST_RANGE[1])
    saturation: float = Field(1.0, ge=C.SATURATION_RANGE[0], le=C.SATURATION_RANGE[1])
    filter: str | None = None
    transition_in: Transition = "none"
    transition_out: Transition = "none"

    @model_validator(mode="after")
    def _end_after_start(self):
        if self.end <= self.start:
            raise ValueError("visual.end phải lớn hơn visual.start")
        return self

    @property
    def source_duration(self) -> float:
        return self.end - self.start

    @property
    def timeline_duration(self) -> float:
        return self.source_duration / self.speed


class Section(BaseModel):
    srt_index: int
    voice_start: float
    voice_end: float
    # Danh sách thay vì 1 visual: cue dài hơn 8s phải tách thành nhiều clip.
    visuals: list[Visual] = Field(min_length=1)
    subtitle: str = ""      # tuỳ chọn: repair_service tự điền từ cue (tiết kiệm token output)
    purpose: Purpose


class EditPlan(BaseModel):
    creative_direction: str
    voice_language: str
    target_duration_seconds: int
    sections: list[Section] = Field(min_length=1)
