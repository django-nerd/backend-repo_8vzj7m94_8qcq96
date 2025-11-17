"""
Database Schemas for CRE Capital Stack Optimizer

Each Pydantic model represents a MongoDB collection. The collection name is the lowercase
of the class name (e.g., Project -> "project").
"""
from typing import Optional, List, Literal
from pydantic import BaseModel, Field

# Core entities
class Project(BaseModel):
    name: str = Field(..., description="Project name")
    location: Optional[str] = Field(None, description="City, State")
    tdc: float = Field(..., gt=0, description="Total Development Cost ($)")
    noi: float = Field(..., ge=0, description="Stabilized Net Operating Income (annual $)")
    min_dscr: float = Field(1.25, gt=0, description="Minimum DSCR covenant")
    max_ltc: float = Field(0.65, gt=0, le=1, description="Maximum loan-to-cost for senior debt")
    min_equity: float = Field(0.1, ge=0, le=1, description="Minimum required common equity as % of TDC")

class CapitalOption(BaseModel):
    name: str = Field(..., description="Instrument name, e.g., Senior Debt, Mezzanine, Pref Equity, Common Equity")
    kind: Literal["debt","mezz","pref","equity"] = Field(..., description="Type category")
    annual_cost: float = Field(..., gt=0, description="Annual cost of capital as a decimal (e.g., 0.07 = 7%)")
    points: float = Field(0.0, ge=0, description="One-time origination points as decimal of principal")
    min_share: float = Field(0.0, ge=0, le=1, description="Minimum share of TDC this instrument must represent")
    max_share: float = Field(1.0, gt=0, le=1, description="Maximum share of TDC this instrument can represent")
    max_ltc: Optional[float] = Field(None, ge=0, le=1, description="For senior/mezz debt: max LTC limit of TDC")
    enforce_dscr: bool = Field(False, description="Include this instrument in DSCR debt service constraint")

class StackSlice(BaseModel):
    option_name: str
    kind: str
    amount: float
    share: float
    annual_cost: float

class CapitalStack(BaseModel):
    project_name: str
    tdc: float
    wacc: float
    slices: List[StackSlice]
    notes: Optional[str] = None

# The schema endpoint reader uses these models to understand collections.
