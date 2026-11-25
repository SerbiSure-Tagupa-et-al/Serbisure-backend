"""Lightweight offline barangay & municipality proximity matching for Region X / CDO.

Uses only Python's standard library (math, re). Coordinates represent barangay/municipality
centers so exact household GPS pins are never stored or exposed (RA 10173 privacy-safe).
"""
import math
import re

EARTH_RADIUS_KM = 6371.0

# All 80 Cagayan de Oro City barangays (approximate center coordinates).
CDO_BARANGAY_COORDS: dict[str, tuple[float, float]] = {
    # Poblacion Barangays 1–40 (compact urban core around Divisoria / Gaston Park)
    'barangay 1': (8.4765, 124.6422),
    'barangay 2': (8.4774, 124.6431),
    'barangay 3': (8.4779, 124.6438),
    'barangay 4': (8.4783, 124.6442),
    'barangay 5': (8.4787, 124.6448),
    'barangay 6': (8.4771, 124.6444),
    'barangay 7': (8.4763, 124.6438),
    'barangay 8': (8.4776, 124.6451),
    'barangay 9': (8.4782, 124.6456),
    'barangay 10': (8.4791, 124.6459),
    'barangay 11': (8.4795, 124.6464),
    'barangay 12': (8.4789, 124.6470),
    'barangay 13': (8.4810, 124.6472),
    'barangay 14': (8.4801, 124.6468),
    'barangay 15': (8.4820, 124.6465),
    'barangay 16': (8.4828, 124.6476),
    'barangay 17': (8.4836, 124.6481),
    'barangay 18': (8.4824, 124.6494),
    'barangay 19': (8.4811, 124.6489),
    'barangay 20': (8.4804, 124.6496),
    'barangay 21': (8.4826, 124.6512),
    'barangay 22': (8.4841, 124.6524),
    'barangay 23': (8.4832, 124.6532),
    'barangay 24': (8.4821, 124.6537),
    'barangay 25': (8.4813, 124.6544),
    'barangay 26': (8.4803, 124.6548),
    'barangay 27': (8.4795, 124.6536),
    'barangay 28': (8.4786, 124.6529),
    'barangay 29': (8.4779, 124.6523),
    'barangay 30': (8.4772, 124.6516),
    'barangay 31': (8.4798, 124.6512),
    'barangay 32': (8.4790, 124.6504),
    'barangay 33': (8.4784, 124.6494),
    'barangay 34': (8.4776, 124.6486),
    'barangay 35': (8.4768, 124.6499),
    'barangay 36': (8.4762, 124.6511),
    'barangay 37': (8.4757, 124.6520),
    'barangay 38': (8.4751, 124.6502),
    'barangay 39': (8.4748, 124.6489),
    'barangay 40': (8.4745, 124.6476),
    # 40 Named Cagayan de Oro Barangays
    'agusan': (8.4925, 124.7429),
    'baikingon': (8.4272, 124.5882),
    'balubal': (8.4767, 124.7690),
    'balulang': (8.4525, 124.6295),
    'bayabas': (8.5026, 124.6443),
    'bayanga': (8.3736, 124.6119),
    'besigan': (8.2947, 124.5635),
    'bonbon': (8.5078, 124.6497),
    'bugo': (8.5108, 124.7564),
    'bulua': (8.5067, 124.6119),
    'camaman-an': (8.4664, 124.6562),
    'canito-an': (8.4706, 124.6085),
    'carmen': (8.4816, 124.6377),
    'consolacion': (8.4856, 124.6490),
    'cugman': (8.4748, 124.7145),
    'dansolihon': (8.3489, 124.6172),
    'f. s. catanico': (8.4581, 124.7064),
    'f.s. catanico': (8.4581, 124.7064),
    'fs catanico': (8.4581, 124.7064),
    'gusa': (8.4793, 124.6828),
    'indahag': (8.4392, 124.6540),
    'iponan': (8.5023, 124.5976),
    'kauswagan': (8.4969, 124.6401),
    'lapasan': (8.4847, 124.6644),
    'lumbia': (8.4046, 124.6114),
    'macabalan': (8.4932, 124.6579),
    'macasandig': (8.4608, 124.6448),
    'mambuaya': (8.3596, 124.6028),
    'nazareth': (8.4713, 124.6476),
    'pagalungan': (8.3965, 124.5681),
    'pagatpat': (8.4638, 124.6033),
    'patag': (8.4926, 124.6226),
    'pigsag-an': (8.3532, 124.5174),
    'puerto': (8.4968, 124.7568),
    'puntod': (8.4906, 124.6552),
    'san simon': (8.4524, 124.5764),
    'tablon': (8.4831, 124.7314),
    'taglimao': (8.3792, 124.5446),
    'tagpangi': (8.3746, 124.5582),
    'tignapoloan': (8.3204, 124.5476),
    'tuburan': (8.3665, 124.5318),
    'tumpagon': (8.3388, 124.5046),
}

# City & municipality fallback coordinates for Cagayan de Oro and Misamis Oriental.
REGION_X_CITY_COORDS: dict[str, tuple[float, float]] = {
    'cagayan de oro': (8.4772, 124.6459),
    'opol': (8.5193, 124.5724),
    'el salvador': (8.5631, 124.5228),
    'tagoloan': (8.5378, 124.7536),
    'villanueva': (8.5872, 124.7775),
    'jasaan': (8.6544, 124.7544),
    'alubijid': (8.5714, 124.4744),
    'claveria': (8.6100, 124.8947),
    'gingoog': (8.8244, 125.1028),
    'laguindingan': (8.5892, 124.4428),
    'initao': (8.5008, 124.3036),
    'balingasag': (8.7442, 124.7789),
    'gitagum': (8.5989, 124.4050),
    'libertad': (8.5653, 124.3514),
    'lugait': (8.3414, 124.2583),
    'manticao': (8.4044, 124.2858),
    'naawan': (8.4336, 124.2903),
    'medina': (8.9125, 125.0233),
    'salay': (8.8603, 124.7897),
    'balingoan': (9.0006, 124.8472),
    'binuangan': (8.9167, 124.7908),
    'lagonglong': (8.8031, 124.7917),
    'magsaysay': (9.0206, 125.1897),
    'sugbongcogon': (8.9478, 124.7889),
    'talisayan': (8.9942, 124.8819),
    'iligan': (8.2280, 124.2452),
    'malaybalay': (8.1575, 125.1278),
    'valencia': (7.9064, 125.0942),
}

_BARANGAY_ALIASES = {
    'camamanan': 'camaman-an',
    'canitoan': 'canito-an',
    'pigsagan': 'pigsag-an',
    'fs catanico': 'f.s. catanico',
    'f s catanico': 'f.s. catanico',
    'catanico': 'f.s. catanico',
}


def normalize_city(name: str | None) -> str:
    """Normalize Philippine city/municipality names for reliable comparison."""
    raw = re.sub(r'\s+', ' ', (name or '').strip().lower())
    if not raw:
        return ''
    raw = re.sub(r'\s*\(.*?\)\s*', ' ', raw).strip()
    raw = re.sub(r'^city of\s+', '', raw)
    raw = re.sub(r'^municipality of\s+', '', raw)
    raw = re.sub(r'\s+city$', '', raw)
    raw = re.sub(r'\s+', ' ', raw).strip()
    if raw in ('cdo', 'cagayan de oro', 'cagayan'):
        return 'cagayan de oro'
    return raw


def normalize_barangay(name: str | None) -> str:
    """Normalize barangay names (preserving numbered Poblacion barangays 1..40)."""
    raw = re.sub(r'\s+', ' ', (name or '').strip().lower())
    if not raw:
        return ''
    raw = re.sub(r'\s*\(pob\.?\)$', '', raw).strip()
    numbered = re.match(r'^(?:brgy\.?|barangay)\s*0*(\d+)$', raw)
    if numbered:
        return f'barangay {int(numbered.group(1))}'
    if re.fullmatch(r'0*\d+', raw):
        return f'barangay {int(raw)}'
    stripped = re.sub(r'^(?:brgy\.?|barangay)\s+', '', raw).strip()
    stripped = re.sub(r'\s*-\s*', '-', stripped)
    return _BARANGAY_ALIASES.get(stripped.replace('.', ''), stripped)


def resolve_coordinates(barangay: str | None, city: str | None) -> tuple[float, float] | None:
    """Return (lat, lon) for a barangay/city, preferring exact CDO barangay centers."""
    norm_city = normalize_city(city)
    norm_brgy = normalize_barangay(barangay)
    if norm_brgy and (not norm_city or norm_city == 'cagayan de oro'):
        coord = CDO_BARANGAY_COORDS.get(norm_brgy)
        if coord is not None:
            return coord
    if norm_city:
        return REGION_X_CITY_COORDS.get(norm_city)
    return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometers between two WGS84 points."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return EARTH_RADIUS_KM * c


def compute_proximity(
    origin_barangay: str | None,
    origin_city: str | None,
    target_barangay: str | None,
    target_city: str | None,
) -> tuple[float | None, str | None]:
    """Compute approximate distance (km) and a plain-English proximity label."""
    o_brgy = normalize_barangay(origin_barangay)
    t_brgy = normalize_barangay(target_barangay)
    o_city = normalize_city(origin_city)
    t_city = normalize_city(target_city)

    cities_conflict = bool(o_city and t_city and o_city != t_city)
    if o_brgy and t_brgy and o_brgy == t_brgy and not cities_conflict:
        return 0.0, 'Same Barangay'

    origin_coords = resolve_coordinates(origin_barangay, origin_city)
    target_coords = resolve_coordinates(target_barangay, target_city)
    if origin_coords is not None and target_coords is not None:
        raw_km = haversine_km(origin_coords[0], origin_coords[1], target_coords[0], target_coords[1])
        km = round(raw_km, 1)
        if km < 0.5:
            return km, '<1 km away'
        return km, f'~{km:.1f} km away'

    if o_city and t_city and o_city == t_city:
        return None, 'Same City'
    return None, None
