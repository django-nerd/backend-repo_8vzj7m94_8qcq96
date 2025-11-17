import os
from typing import List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from database import db, create_document, get_documents
from schemas import Project, CapitalOption, CapitalStack, StackSlice

app = FastAPI(title="CRE Capital Stack Optimizer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class OptimizeRequest(BaseModel):
    project: Project
    options: List[CapitalOption]
    granularity: float = 0.01  # step for shares (1%)


def compute_dscr(noi: float, annual_debt_service: float) -> float:
    if annual_debt_service == 0:
        return float("inf")
    return noi / annual_debt_service


def optimize_stack(project: Project, options: List[CapitalOption], granularity: float = 0.01) -> CapitalStack:
    # Basic heuristic optimizer: allocate cheapest capital first subject to constraints
    tdc = project.tdc

    # Sort options by effective cost including points amortized over 1 year (simplified)
    def eff_cost(opt: CapitalOption):
        return opt.annual_cost + opt.points

    sorted_opts = sorted(options, key=eff_cost)

    remaining = tdc
    allocations = []  # (option, amount)

    # First pass: respect min_share of each option
    for opt in sorted_opts:
        min_amt = max(opt.min_share * tdc, 0)
        if min_amt > 0:
            max_cap = opt.max_share * tdc
            use = min(min_amt, max_cap, remaining)
            if use > 0:
                allocations.append((opt, use))
                remaining -= use

    # Second pass: fill remaining with cheapest options within constraints
    for opt in sorted_opts:
        if remaining <= 1e-6:
            break
        current_amt = sum(a for o, a in allocations if o.name == opt.name)
        max_cap = opt.max_share * tdc
        addable = max_cap - current_amt
        if opt.max_ltc is not None:
            addable = min(addable, opt.max_ltc * tdc - current_amt)
        add = min(addable, remaining)
        if add > 1e-6:
            allocations.append((opt, add))
            remaining -= add

    # Ensure equity minimum
    common_equity = next((o for o in options if o.kind == "equity"), None)
    if common_equity:
        current_equity = sum(a for o, a in allocations if o.name == common_equity.name)
        min_equity_amt = max(project.min_equity * tdc, common_equity.min_share * tdc)
        if current_equity < min_equity_amt:
            needed = min_equity_amt - current_equity
            space = common_equity.max_share * tdc - current_equity
            adj = min(needed, space)
            if adj > 0:
                allocations.append((common_equity, adj))
                remaining -= adj

    # If still remaining due to constraints, force equity to fill
    if remaining > 1e-6:
        if not common_equity:
            raise HTTPException(status_code=400, detail="No equity option to complete the stack")
        space = common_equity.max_share * tdc - sum(a for o, a in allocations if o.name == common_equity.name)
        add = min(space, remaining)
        if add <= 0:
            raise HTTPException(status_code=400, detail="Constraints too tight to fully fund the stack")
        allocations.append((common_equity, add))
        remaining -= add

    # DSCR and LTC constraints: adjust if needed by capping debt
    total_annual_debt = 0.0
    total_debt_amount = 0.0
    for o, a in allocations:
        if o.kind in ("debt", "mezz") and o.enforce_dscr:
            total_annual_debt += a * o.annual_cost
        if o.kind in ("debt", "mezz"):
            total_debt_amount += a

    # Enforce max LTC for senior debt if provided at project level
    if project.max_ltc is not None:
        if total_debt_amount > project.max_ltc * tdc:
            # reduce debt proportionally and shift to equity
            reduce_ratio = (total_debt_amount - project.max_ltc * tdc) / max(total_debt_amount, 1e-9)
            for idx, (o, a) in enumerate(list(allocations)):
                if o.kind in ("debt", "mezz"):
                    reduce_amt = a * reduce_ratio
                    allocations[idx] = (o, a - reduce_amt)
                    remaining += reduce_amt
            # fill remaining with equity
            space = common_equity.max_share * tdc - sum(a for o, a in allocations if o.name == common_equity.name)
            add = min(space, remaining)
            if add < remaining - 1e-6:
                raise HTTPException(status_code=400, detail="Equity caps too tight to replace reduced debt")
            allocations.append((common_equity, add))
            remaining -= add

    # Enforce DSCR
    dscr = compute_dscr(project.noi, total_annual_debt)
    if dscr < project.min_dscr:
        # Need to lower debt service by reducing DSCR-enforced debt and replacing with equity
        required_annual_debt = project.noi / project.min_dscr
        delta_annual = max(total_annual_debt - required_annual_debt, 0)
        # Reduce from highest cost DSCR-enforced debt first
        dscr_debts = sorted([(o, a) for o, a in allocations if o.kind in ("debt", "mezz") and o.enforce_dscr], key=lambda x: -x[0].annual_cost)
        for idx, (o, a) in enumerate(list(dscr_debts)):
            if delta_annual <= 1e-6:
                break
            reducible_annual = a * o.annual_cost
            take = min(reducible_annual, delta_annual)
            take_principal = take / o.annual_cost
            # apply reduction in main list
            for j, (oo, aa) in enumerate(allocations):
                if oo.name == o.name and abs(aa - a) < 1e-9:
                    allocations[j] = (oo, aa - take_principal)
                    break
            delta_annual -= take
            remaining += take_principal
        # fill with equity
        space = common_equity.max_share * tdc - sum(a for o, a in allocations if o.name == common_equity.name)
        add = min(space, remaining)
        if add < remaining - 1e-6:
            raise HTTPException(status_code=400, detail="Equity caps too tight to hit DSCR")
        allocations.append((common_equity, add))
        remaining -= add

    # Aggregate by option
    agg = {}
    for o, a in allocations:
        if a <= 1e-6:
            continue
        key = (o.name, o.kind, o.annual_cost)
        agg.setdefault(key, 0.0)
        agg[key] += a

    slices: List[StackSlice] = []
    total_cost_annual = 0.0
    for (name, kind, annual_cost), amount in agg.items():
        share = amount / tdc
        total_cost_annual += amount * annual_cost
        slices.append(StackSlice(option_name=name, kind=kind, amount=amount, share=share, annual_cost=annual_cost))

    wacc = total_cost_annual / tdc

    return CapitalStack(
        project_name=project.name,
        tdc=tdc,
        wacc=wacc,
        slices=slices,
        notes="Heuristic allocation - minimizes cost subject to DSCR/LTC/equity constraints."
    )


@app.post("/api/optimize", response_model=CapitalStack)
def api_optimize(req: OptimizeRequest):
    stack = optimize_stack(req.project, req.options, req.granularity)
    # Persist the result
    create_document("capitalstack", stack.model_dump())
    return stack


@app.get("/api/history")
def api_history(limit: int = 20):
    docs = get_documents("capitalstack", limit=limit)
    # convert ObjectIds to strings
    def safe(d):
        d = dict(d)
        d["_id"] = str(d.get("_id"))
        return d
    return [safe(x) for x in docs]


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"
    return response


@app.get("/")
def read_root():
    return {"message": "CRE Capital Stack Optimizer Backend"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
