"""Pydantic domain models shared between transformation, storage, and handlers."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, StringConstraints, model_validator

PRIVATE_TIER_ORDER = ["Essential", "Classic", "Premium", "Max"]
CORPORATE_TIER_ORDER = ["S", "M", "L", "XL"]


class VenueAddress(BaseModel):
    street: str = ""
    postal_code: str = ""
    city: str = ""


class VisitLimits(BaseModel):
    private: dict[str, int | None]
    corporate: dict[str, int | None]


class Venue(BaseModel):
    name: str
    slug: str
    url: str
    tiers_private: list[str]
    tiers_corporate: list[str]
    min_tier_private: str | None
    min_tier_corporate: str | None
    activities: list[str]
    district: str
    street: str
    is_plus: bool
    address_id: str
    lat: float | None
    lng: float | None
    address: VenueAddress
    rating: float | None
    review_count: int | None
    is_online: bool
    has_coordinates: bool
    # Enrichment fields (nullable until filled by the background task)
    visit_limits: VisitLimits | None = None


class VenueDetail(BaseModel):
    """Enrichment payload returned by /api/venues/{id}."""

    visit_limits: VisitLimits | None = None
    importantInfo: str | None = None
    phone: str | None = None
    website: str | None = None
    description: str | None = None
    fetched_at: float


class TierConfig(BaseModel):
    private: dict
    corporate: dict


class VenuesPayload(BaseModel):
    """Response shape for /api/venues."""

    fetched_at: float
    total_venues: int
    venues_with_coords: int
    tier_config: TierConfig
    venues: list[Venue]


class Course(BaseModel):
    id: int
    date: str
    title: str
    start_time: str
    end_time: str
    venue_id: str
    venue_name: str
    lat: float | None
    lng: float | None
    district: str
    category: str
    category_id: int | None
    teacher: str
    free_spots: int | None
    max_spots: int | None
    is_online: bool
    is_plus: bool
    # Class-level plan coverage; can be stricter than the venue's.
    tiers_private: list[str] = []
    tiers_corporate: list[str] = []
    min_tier_private: str | None = None
    min_tier_corporate: str | None = None
    # On-site surcharge: USC's own text, and the amount parsed from it or the title.
    extra_fee_text: str | None = None
    extra_fee_eur: int | None = None


class CourseDetail(BaseModel):
    """Lazily fetched extras for one course, returned by /api/courses/{id}."""

    description: str = ""
    rating: float | None = None
    rating_count: int | None = None
    free_cancellation_hours: int | None = None
    booking_type: str | None = None
    fetched_at: float


class CourseFetchError(BaseModel):
    date: str
    reason: str
    city_id: int | None = None


class City(BaseModel):
    """Row in the cities table, populated from USC /cities."""

    id: int
    name: str
    country_code: str | None = None
    centroid_lat: float | None = None
    centroid_lng: float | None = None
    venue_address_count: int | None = None
    # Lazily derived bbox from venue coordinates (None until first venue fetch).
    lat_min: float | None = None
    lat_max: float | None = None
    lng_min: float | None = None
    lng_max: float | None = None


class CitiesResponse(BaseModel):
    """Response shape for GET /api/cities."""

    cities: list[City]
    default_city_id: int


class CityVenuesEntry(BaseModel):
    """One city's slice of a multi-city venues payload."""

    city_id: int
    city_name: str
    fetched_at: float
    total: int
    with_coordinates: int
    venues: list[Venue]


class MultiCityVenuesPayload(BaseModel):
    """Response shape for GET /api/venues (city-grouped)."""

    cities: list[CityVenuesEntry]
    tier_config: TierConfig


class CityCoursesEntry(BaseModel):
    """One city's slice of a multi-city courses payload."""

    city_id: int
    city_name: str
    date_from: str
    date_to: str
    total: int
    courses: list[Course]


class MultiCityCoursesResponse(BaseModel):
    """Response shape for GET /api/courses (city-grouped)."""

    cities: list[CityCoursesEntry]
    date_from: str
    date_to: str
    errors: list[CourseFetchError]


class Plan(BaseModel):
    """The user's own USC membership; drives badges, independent of the tier filter."""

    type: Literal["corporate", "private"]
    tier: str

    @model_validator(mode="after")
    def _tier_matches_type(self) -> Self:
        order = CORPORATE_TIER_ORDER if self.type == "corporate" else PRIVATE_TIER_ORDER
        if self.tier not in order:
            raise ValueError(f"tier must be one of {order} for a {self.type} plan")
        return self


class Home(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    label: str = ""


class Settings(BaseModel):
    """Single-user settings stored server-side (see AGENTS.md: single-user assumption)."""

    plan: Plan | None = None
    home: Home | None = None
    bike_kmh: Literal[15, 18, 21] | None = None  # None = default (18)


class GeocodeResult(BaseModel):
    label: str
    lat: float
    lng: float


class TravelEntry(BaseModel):
    """Cached travel data from one origin to one venue (travel table row)."""

    venue_id: str
    bike_km: float | None = None
    bike_at: float | None = None
    pt_min: int | None = None
    pt_lines: list[str] = []
    walk_m: int | None = None
    pt_at: float | None = None
    pt_failed_at: float | None = None


class LatLng(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class TravelRequest(BaseModel):
    venue_ids: list[str] = Field(max_length=500)
    origin: LatLng | None = None  # default: the stored home


class TravelResult(BaseModel):
    bike_km: float | None
    pt_min: int | None
    pt_lines: list[str]
    walk_m: int | None


class TravelResponse(BaseModel):
    results: dict[str, TravelResult]
    pending: list[str]


class ExactTransit(BaseModel):
    pt_min: int
    depart_at: str
    arrive_at: str
    lines: list[str]
    walk_m: int


# ── Planner ──

IsoDate = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]


class Feedback(BaseModel):
    """How a picked course went. Fields only apply in the combinations below."""

    went: Literal["yes", "no", "came_up"]
    no_reason: Literal["cancelled", "full", "didnt_feel_like_it", "other"] | None = None
    rating: int | None = Field(None, ge=1, le=5)
    again: Literal["no", "maybe", "yes"] | None = None
    again_after: Literal["1w", "2-4w", "1-3m", "someday"] | None = None
    note: str = ""
    updated_at: float | None = None

    @model_validator(mode="after")
    def _check_combinations(self) -> Self:
        if self.rating is not None and self.went != "yes":
            raise ValueError("rating only applies when went is 'yes'")
        if self.no_reason is not None and self.went != "no":
            raise ValueError("no_reason only applies when went is 'no'")
        if self.again_after is not None and self.again not in ("maybe", "yes"):
            raise ValueError("again_after only applies when again is 'maybe' or 'yes'")
        return self


class NewSuggestion(BaseModel):
    course_id: int
    date: IsoDate
    rank: int = Field(ge=1)
    reason: str = ""
    tags: list[str] = []


class RunCreate(BaseModel):
    """Body of POST /api/planner/runs."""

    date_from: IsoDate
    date_to: IsoDate
    summary: str = ""
    suggestions: list[NewSuggestion]

    @model_validator(mode="after")
    def _check_window(self) -> Self:
        if self.date_from > self.date_to:
            raise ValueError("date_from must not be after date_to")
        outside = [s.date for s in self.suggestions if not self.date_from <= s.date <= self.date_to]
        if outside:
            raise ValueError(f"suggestion dates outside the run window: {sorted(set(outside))}")
        return self


class Run(BaseModel):
    id: int
    created_at: float
    date_from: str
    date_to: str
    summary: str


class Suggestion(BaseModel):
    """A ranked suggestion with a snapshot of its course (the course cache gets purged)."""

    id: int
    run_id: int
    course_id: int
    date: str
    rank: int
    reason: str
    tags: list[str]
    title: str
    start_time: str
    end_time: str
    venue_id: str
    venue_name: str
    district: str
    category: str
    city_id: int
    lat: float | None
    lng: float | None
    picked: bool
    not_interested: bool
    created_at: float
    feedback: Feedback | None = None
    # Live values joined from the course cache; None once the course is purged.
    free_spots: int | None = None
    max_spots: int | None = None


class SuggestionPatch(BaseModel):
    rank: int | None = Field(None, ge=1)
    reason: str | None = None
    tags: list[str] | None = None
    picked: bool | None = None
    not_interested: bool | None = None


class Profile(BaseModel):
    text: str
    updated_at: float | None = None


class ProfileUpdate(BaseModel):
    text: str


class PlannerSuggestionsResponse(BaseModel):
    """Response shape for GET /api/planner/suggestions and /history."""

    runs: list[Run]
    suggestions: list[Suggestion]


class RunCreated(BaseModel):
    run: Run
    suggestions: list[Suggestion]
