"""Pydantic domain models shared between transformation, storage, and handlers."""

from pydantic import BaseModel


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
    bookingLimitsText: str | None = None


class VenueDetail(BaseModel):
    """Enrichment payload returned by /api/venues/{id}."""

    visit_limits: VisitLimits | None = None
    bookingLimitsText: str | None = None
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


class CourseFetchError(BaseModel):
    date: str
    reason: str
    city_id: int | None = None


class CoursesResponse(BaseModel):
    """Response shape for /api/courses."""

    courses: list[Course]
    date_from: str
    date_to: str
    total: int
    errors: list[CourseFetchError]


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
