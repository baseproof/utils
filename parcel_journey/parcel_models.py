"""
Pydantic models capturing the full Jefferson County (eringcapture.jccal.org)
parcel detail page — every section, including the expandable
'Historical Tax Payments' and 'Legal Instruments' tables.

Monetary amounts and dates are kept as strings ('$105,800', '12/29/2023')
to preserve exactly what the page shows; parse downstream if you need numbers.
Fields default to '' (empty string) instead of Optional/None because
Vertex AI structured output rejects anyOf(null) unions in JSON schemas.
"""

from pydantic import BaseModel, Field


# ---------- Identity & location ----------

class PropertyAddress(BaseModel):
    street: str = Field('', description="e.g. '2984 SARTAIN DR'")
    city_state_zip: str = Field('', description="e.g. 'ADAMSVILLE AL 35005'")


class ParcelIdentity(BaseModel):
    parcel_number: str = Field('', description="e.g. '20 00 01 1 001 003.000'")
    tax_year: str = ''
    owner: str = Field('', description="Owner name as shown, e.g. 'SPRUELL THERON C & SHEILA A'")
    property_address: PropertyAddress = Field(default_factory=lambda: PropertyAddress())
    mailing_address: PropertyAddress = Field(default_factory=lambda: PropertyAddress())
    neighborhood: str = ''
    subdivision: str = ''
    book: str = ''
    page: str = ''
    lot: str = ''
    acreage: str = ''
    section: str = ''
    township: str = ''
    range: str = ''


# ---------- Assessment header ----------

class AssessmentInfo(BaseModel):
    appraised_value: str = Field('', description="Headline appraised value, e.g. '$105,800'")
    tax_year: str = ''
    property_class: str = ''
    exempt_code: str = ''
    municipality: str = ''
    school_district: str = ''
    disability_code: str = ''
    over_65_code: str = ''
    total_hc_area: str = Field('', description="Total heated/cooled area, e.g. '1000 Sqft'")
    metes_and_bounds: str = ''
    remarks: str = ''


# ---------- Valuation ----------

class ValuationSummary(BaseModel):
    total_improvement_value: str = ''
    total_land_value: str = ''
    total_market_value: str = ''
    total_appraised_value: str = ''
    assessed_value: str = ''
    prior_year_total_improvement_value: str = ''
    prior_year_total_land_value: str = ''
    prior_year_total_appraised_value: str = ''
    bill_number: str = Field('', description="e.g. '5236794 - REAL PROPERTY'")


# ---------- Tax breakdown ----------

class TaxBreakdownRow(BaseModel):
    year: str = ''
    millage_type: str = Field('', description="e.g. 'STATE', 'COUNTY', 'SCHOOL', 'SPC SCHOOL1'")
    property_class: str = ''
    municipality: str = ''
    assessed_value: str = ''
    tax: str = ''
    exemption: str = ''
    tax_exemption: str = ''
    total_tax: str = ''


class TaxBreakdown(BaseModel):
    rows: list[TaxBreakdownRow] = Field(default_factory=list)
    fees_and_interest: str = ''
    tax_total: str = Field('', description="'Tax Totals' grand total, e.g. '$1,060.12'")
    tax_column_total: str = Field('', description="Sum of the 'Tax' column in the Tax Totals row (differs from tax_total when exemptions apply)")
    tax_total_exemption: str = ''


# ---------- Tax payments ----------

class TaxPaymentRow(BaseModel):
    tax_year: str = ''
    paid_by: str = ''
    paid_date: str = Field('', description="As shown, e.g. '12/29/2023'")
    receipt_number: str = ''
    total_taxes_plus_fees: str = ''
    total_paid: str = ''


class TaxPayments(BaseModel):
    current: list[TaxPaymentRow] = Field(default_factory=list, description="'Tax Payment Information' table")
    historical: list[TaxPaymentRow] = Field(
        default_factory=list,
        description="'Historical Tax Payment Information' table, revealed by clicking 'View Historical Tax Payments'",
    )


# ---------- Land ----------

class LandInformationRow(BaseModel):
    appraisal_type: str = ''
    property_class: str = ''
    land_use: str = Field('', description="e.g. '111 - HOUSEHOLD UNITS'")
    acreage: str = ''
    square_foot: str = ''
    lot_size: str = Field('', description="e.g. '100S X 326S IRR'")
    market_value: str = ''
    current_use_value: str = ''


# ---------- Building ----------

class BuildingGeneralInfo(BaseModel):
    building: str = Field('', description="Building number, e.g. '001'")
    building_type: str = Field('', description="e.g. '111 - SINGLE FAMILY'")
    effective_building_type: str = ''
    year_built: str = ''
    effective_year_built: str = ''
    assessment_class: str = ''
    building_class: str = ''
    number_of_stories: str = ''
    number_of_rooms: str = ''
    heated_cooled_sq_ft: str = ''
    base_area: str = ''
    construction_units: str = ''
    total_adjusted_area: str = ''


class BuildingValue(BaseModel):
    base_rate: str = ''
    adjusted_rate: str = ''
    subtotal: str = ''
    extra_features: str = ''
    base_cost: str = ''
    cost_index: str = ''
    replacement_cost: str = ''
    condition: str = ''
    value: str = ''
    market_adjustment: str = Field('', description="e.g. '0%'")
    final_value: str = ''
    miscellaneous_improvement_value: str = ''
    total_improvement_value: str = ''


class AppendageRow(BaseModel):
    appendage: str = Field('', description="e.g. 'CP 0.6', 'U 0.4'")
    factor: str = ''
    area: str = ''
    adjusted_area: str = ''


class ConstructionUnitRow(BaseModel):
    category: str = Field('', description="e.g. 'FOUNDATION', 'ROOF TYPE'")
    subcategory: str = Field('', description="e.g. 'WOOD SUBFLOOR', 'HIP-GABLE'")


class ExtraFeatureRow(BaseModel):
    category: str = Field('', description="e.g. 'HEATING & AIR COND.'")
    description: str = Field('', description="e.g. 'HEAT/AC FHA'")


class Building(BaseModel):
    general_info: BuildingGeneralInfo = Field(default_factory=lambda: BuildingGeneralInfo())
    building_value: BuildingValue = Field(default_factory=lambda: BuildingValue())
    appendages: list[AppendageRow] = Field(default_factory=list)
    construction_units: list[ConstructionUnitRow] = Field(default_factory=list)
    extra_features: list[ExtraFeatureRow] = Field(default_factory=list)


# ---------- Deeds, ownership, instruments ----------

class DeedRow(BaseModel):
    sale_date: str = ''
    price: str = ''
    deed: str = ''
    grantor: str = ''
    grantee: str = ''


class OwnershipHistoryRow(BaseModel):
    tax_year: str = ''
    entity_name: str = ''
    mailing_address: str = ''


class LegalInstrumentRow(BaseModel):
    instrument_date: str = Field('', description="e.g. '12/27/1988'")
    instrument_number: str = Field('', description="e.g. '3524-287'")


# ---------- Root model ----------

class ParcelDetail(BaseModel):
    """Complete structured capture of a parcel detail page."""
    identity: ParcelIdentity = Field(default_factory=lambda: ParcelIdentity())
    assessment: AssessmentInfo = Field(default_factory=lambda: AssessmentInfo())
    valuation_summary: ValuationSummary = Field(default_factory=lambda: ValuationSummary())
    tax_breakdown: TaxBreakdown = Field(default_factory=lambda: TaxBreakdown())
    tax_payments: TaxPayments = Field(default_factory=lambda: TaxPayments())
    land_information: list[LandInformationRow] = Field(default_factory=list)
    buildings: list[Building] = Field(default_factory=list, description="One entry per 'Building NNN Information' section")
    deed_information: list[DeedRow] = Field(default_factory=list)
    ownership_history: list[OwnershipHistoryRow] = Field(default_factory=list)
    legal_instruments: list[LegalInstrumentRow] = Field(
        default_factory=list,
        description="Revealed by clicking 'View Instruments'",
    )
