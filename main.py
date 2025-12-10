from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional, List
import httpx

app = FastAPI(title="Renew Power Qualification & Scraping Engine")


# ---------- DATA MODELS ----------

class LeadInput(BaseModel):
    # Basic identity
    name: Optional[str] = None          # maps from Airtable "Lead Name"
    email: Optional[str] = None         # "Email"
    phone: Optional[str] = None         # "Phone"

    # Address
    address: Optional[str] = None       # "Address"
    city: Optional[str] = None          # "City"
    state: Optional[str] = None         # "State"
    zip: Optional[str] = None           # "ZIP"

    # Source & property
    source: Optional[str] = None        # "Source" (Solar IQ, Scraped Property, etc.)
    property_type: Optional[str] = None # "Property Type"
    is_landlord: Optional[bool] = False # from "Is Landlord?" checkbox
    property_count: Optional[int] = 0   # "Property Count"

    # Roof & sun
    roof_type: Optional[str] = None           # "Roof Type"
    roof_age_years: Optional[int] = None      # "Roof Age (Years)"
    shading_level: Optional[str] = None       # "Shading Level"
    hoa_allows_solar: Optional[str] = None    # "HOA Allows Solar?"

    # Distance from Bakersfield (minutes) - approximate
    distance_minutes: Optional[int] = None    # "Distance From Bakersfield (min)"

    # Financials
    # In Airtable we store the range text in "Monthly Bill ($)"
    monthly_bill_raw: Optional[str] = None    # raw string e.g. "$200–$400"
    monthly_bill: Optional[float] = None      # numeric estimate after parsing
    true_up_band: Optional[str] = None        # "True-Up Band"
    credit_band: Optional[str] = None         # "Credit Band"

    # Psychology
    motivation: Optional[str] = None          # "Motivation"
    decision_style: Optional[str] = None      # "Decision Style"


class LeadScore(BaseModel):
    property_score: int
    financial_score: int
    behavioral_score: int
    landlord_score: int
    ai_tier: str
    buyer_type: str
    pain_points: List[str]
    reject_reasons: List[str]


# ---------- HELPER FUNCTIONS (SCRAPING / ENRICHMENT HOOKS) ----------

async def enrich_with_property_data(lead: LeadInput) -> LeadInput:
    """
    This is where we will later plug in:
    - property APIs (Zillow, Estated, etc.)
    - permit databases
    - GIS/satellite services
    - drive-time / distance services

    For now this just returns the lead unchanged,
    so we can deploy the full structure and upgrade it later.
    """
    # Example future structure (pseudocode):
    # if lead.address and lead.city and lead.state:
    #     async with httpx.AsyncClient() as client:
    #         resp = await client.get(
    #             "https://some-property-api.com/lookup",
    #             params={
    #                 "address": lead.address,
    #                 "city": lead.city,
    #                 "state": lead.state,
    #             },
    #         )
    #     data = resp.json()
    #     if not lead.roof_type and "roof_type" in data:
    #         lead.roof_type = data["roof_type"]
    #     ...

    return lead


def normalize_monthly_bill(lead: LeadInput) -> None:
    """
    Turn a range string like "$200-$400" into a numeric estimate.
    If monthly_bill is already numeric, leave it.
    """
    if lead.monthly_bill is not None:
        return

    if not lead.monthly_bill_raw:
        return

    raw = lead.monthly_bill_raw.replace("$", "").replace(" ", "").replace("–", "-")
    # Look for ranges like 200-400
    if "-" in raw:
        parts = raw.split("-")
        try:
            low = float(parts[0])
            high = float(parts[1])
            lead.monthly_bill = (low + high) / 2.0
            return
        except ValueError:
            pass

    # Single value
    try:
        lead.monthly_bill = float(raw)
    except ValueError:
        pass


def map_shading_to_code(shading_level: Optional[str]) -> Optional[str]:
    if not shading_level:
        return None
    s = shading_level.lower()
    if "full" in s:
        return "full_sun"
    if "mostly" in s:
        return "mostly_sunny"
    if "partial" in s:
        return "partial_shade"
    if "heavy" in s:
        return "heavy_shade"
    return "unknown"


def map_hoa_to_bool(hoa: Optional[str]) -> Optional[bool]:
    if not hoa:
        return None
    s = hoa.lower()
    if "no hoa" in s:
        return True
    if "allow" in s:
        return True
    if "restriction" in s:
        return False
    return None  # not sure / unknown


def map_credit_band(credit_band: Optional[str]) -> str:
    if not credit_band:
        return "Unknown"
    return credit_band


def map_true_up_band(true_up_band: Optional[str]) -> str:
    if not true_up_band:
        return "Unknown"
    return true_up_band


def normalize_decision_style(style: Optional[str]) -> str:
    if not style:
        return "Unknown"
    return style


def normalize_motivation(m: Optional[str]) -> str:
    if not m:
        return "Other/Unknown"
    return m


# ---------- CORE SCORING LOGIC ----------

def apply_scoring(lead: LeadInput) -> LeadScore:
    """
    Apply Marshall-style qualification rules + scoring.
    """
    reject_reasons: List[str] = []

    # Normalize some derived values
    normalize_monthly_bill(lead)
    shading_code = map_shading_to_code(lead.shading_level)
    hoa_ok = map_hoa_to_bool(lead.hoa_allows_solar)
    cb = map_credit_band(lead.credit_band)
    tub = map_true_up_band(lead.true_up_band).lower()

    # ---- Hard disqualifications
    if lead.distance_minutes is not None and lead.distance_minutes > 90:
        reject_reasons.append("Outside 90-minute radius")

    if lead.roof_type:
        rt = lead.roof_type.lower()
        if "wood" in rt and "shake" in rt:
            reject_reasons.append("Wood shake roof")

    if shading_code == "heavy_shade":
        reject_reasons.append("Excessive shading")

    if lead.monthly_bill is not None and lead.monthly_bill < 150:
        reject_reasons.append("Monthly bill under $150")

    if hoa_ok is False:
        reject_reasons.append("HOA does not allow solar")

    if lead.roof_age_years is not None and lead.roof_age_years >= 15:
        reject_reasons.append("Roof likely needs replacement within 5 years")

    if cb.lower().startswith("under 650"):
        reject_reasons.append("Credit score below 650")

    if reject_reasons:
        return LeadScore(
            property_score=20,
            financial_score=20,
            behavioral_score=40,
            landlord_score=0,
            ai_tier="REJECT",
            buyer_type="Unknown",
            pain_points=[],
            reject_reasons=reject_reasons,
        )

    # ---- Property score
    property_score = 50

    if lead.roof_type:
        rt = lead.roof_type.lower()
        if "asphalt" in rt or "composition" in rt:
            property_score += 25
        elif "tile" in rt or "metal" in rt or "flat" in rt:
            property_score += 15

    if lead.roof_age_years is not None:
        if lead.roof_age_years <= 5:
            property_score += 20
        elif lead.roof_age_years <= 10:
            property_score += 10

    if shading_code == "full_sun":
        property_score += 15
    elif shading_code == "mostly_sunny":
        property_score += 10
    elif shading_code == "partial_shade":
        property_score += 0

    property_score = max(0, min(property_score, 100))

    # ---- Financial score
    financial_score = 40

    if lead.monthly_bill is not None:
        if lead.monthly_bill >= 400:
            financial_score += 30
        elif lead.monthly_bill >= 200:
            financial_score += 20
        elif lead.monthly_bill >= 150:
            financial_score += 10

    if "500+" in tub:
        financial_score += 20
    elif "under 500" in tub:
        financial_score += 10

    if cb.lower().startswith("720"):
        financial_score += 20
    elif "650–719" in cb or "650-719" in cb:
        financial_score += 10

    financial_score = max(0, min(financial_score, 100))

    # ---- Behavioral score / buyer type
    behavioral_score = 50
    buyer_type = "Unknown"
    pain_points: List[str] = []

    style = normalize_decision_style(lead.decision_style)
    s = style.lower()
    if "research" in s or "quality" in s:
        behavioral_score += 25
        buyer_type = "Quality Focused"
    elif "trust" in s and "expert" in s:
        behavioral_score += 20
        buyer_type = "Trusts Experts"
    elif "price" in s:
        behavioral_score -= 10
        buyer_type = "Price Shopper"
    elif "deal" in s:
        behavioral_score -= 5
        buyer_type = "Discount Seeker"

    mot = normalize_motivation(lead.motivation).lower()
    if "saving" in mot:
        pain_points.append("high_bills")
    if "environment" in mot:
        pain_points.append("environmental_impact")
    if "independence" in mot or "backup" in mot:
        pain_points.append("grid_dependence")
    if "quality" in mot:
        pain_points.append("quality_equipment")

    behavioral_score = max(0, min(behavioral_score, 100))

    # ---- Landlord score
    landlord_score = 0
    if lead.is_landlord:
        landlord_score = 60
        if lead.property_count:
            if lead.property_count >= 10:
                landlord_score = 100
            elif lead.property_count >= 6:
                landlord_score = 85
            elif lead.property_count >= 3:
                landlord_score = 75

    # ---- Weighted total for AI tier
    total_weighted = (
        property_score * 0.4 +
        financial_score * 0.35 +
        behavioral_score * 0.25 +
        (15 if landlord_score >= 75 else 0)
    )

    if total_weighted >= 90:
        ai_tier = "HOT"
    elif total_weighted >= 75:
        ai_tier = "QUALIFIED"
    elif total_weighted >= 60:
        ai_tier = "NURTURE"
    else:
        ai_tier = "REJECT"

    return LeadScore(
        property_score=int(property_score),
        financial_score=int(financial_score),
        behavioral_score=int(behavioral_score),
        landlord_score=int(landlord_score),
        ai_tier=ai_tier,
        buyer_type=buyer_type,
        pain_points=pain_points,
        reject_reasons=reject_reasons,
    )


# ---------- API ENDPOINTS ----------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/score-lead", response_model=LeadScore)
async def score_lead(lead: LeadInput):
    # Step 1: enrichment hook (later we'll add APIs here)
    enriched_lead = await enrich_with_property_data(lead)
    # Step 2: scoring
    result = apply_scoring(enriched_lead)
    return result
