"""What a nurse records, by assessment type and by stage of the visit.

This is the one place that says which vitals exist, which belong to which assessment type and which are asked at
each stage. The nurse screen is built from `config()`, and the API checks every saved note against the same rules.

The sets below follow public clinical references (WHO, MSF and national guidance) and are meant to be approved by
the clinic's clinical lead. They do not set alert thresholds or reference ranges: those are the clinic's to decide.
"""

import re
from decimal import Decimal, InvalidOperation

NUMBER = "number"
CHOICE = "choice"
BP = "bp"

# key -> label, unit, kind, and for numbers the widest plausible range (a typing check, not a clinical range)
VITALS = {
    "bp": {"label": "Blood pressure", "unit": "mmHg", "kind": BP, "placeholder": "120/80"},
    "pulse": {"label": "Pulse", "unit": "bpm", "kind": NUMBER, "min": 20, "max": 300},
    "resp_rate": {"label": "Respiratory rate", "unit": "/min", "kind": NUMBER, "min": 3, "max": 120},
    "temp": {"label": "Temperature", "unit": "°C", "kind": NUMBER, "min": 25, "max": 45},
    "spo2": {"label": "SpO₂", "unit": "%", "kind": NUMBER, "min": 30, "max": 100},
    "weight": {"label": "Weight", "unit": "kg", "kind": NUMBER, "min": 0.3, "max": 500},
    "height": {"label": "Height", "unit": "cm", "kind": NUMBER, "min": 20, "max": 260},
    "length": {"label": "Length", "unit": "cm", "kind": NUMBER, "min": 20, "max": 90},
    "bmi": {"label": "BMI", "unit": "kg/m²", "kind": NUMBER, "min": 5, "max": 100, "computed": True},
    "pain": {"label": "Pain score", "unit": "/10", "kind": NUMBER, "min": 0, "max": 10},
    "muac": {"label": "MUAC", "unit": "mm", "kind": NUMBER, "min": 40, "max": 400},
    "head_circ": {"label": "Head circumference", "unit": "cm", "kind": NUMBER, "min": 20, "max": 70},
    "glucose": {"label": "Blood glucose", "unit": "mmol/L", "kind": NUMBER, "min": 0.5, "max": 60},
    "gestational_age": {"label": "Gestational age", "unit": "weeks", "kind": NUMBER, "min": 1, "max": 45},
    "fundal_height": {"label": "Fundal height", "unit": "cm", "kind": NUMBER, "min": 5, "max": 60},
    "fetal_heart_rate": {"label": "Fetal heart rate", "unit": "bpm", "kind": NUMBER, "min": 60, "max": 220},
    "urine_protein": {
        "label": "Urine protein",
        "unit": "",
        "kind": CHOICE,
        "choices": ["Negative", "Trace", "+", "++", "+++", "++++"],
    },
    "oedema": {"label": "Oedema", "unit": "", "kind": CHOICE, "choices": ["None", "Mild", "Moderate", "Severe"]},
}

ASSESSMENTS = {
    "general_adult": {
        "label": "General adult",
        "vitals": ["bp", "pulse", "resp_rate", "temp", "spo2", "weight", "height", "bmi", "pain"],
    },
    "child": {
        "label": "Child",
        "vitals": ["temp", "pulse", "resp_rate", "weight", "height", "muac", "spo2", "glucose"],
        # Respiratory rate in children is counted for a full minute.
        "hints": {"resp_rate": "Count for a full minute"},
    },
    "newborn": {
        "label": "Newborn",
        "vitals": ["weight", "temp", "resp_rate", "pulse", "length", "head_circ", "glucose"],
        "labels": {"pulse": "Heart rate"},
    },
    "antenatal": {
        "label": "Antenatal",
        "vitals": [
            "bp", "weight", "bmi", "urine_protein", "oedema", "gestational_age", "fundal_height", "fetal_heart_rate",
        ],
        "hints": {"fundal_height": "Measured from 24 weeks"},
    },
    "chronic": {
        "label": "Chronic follow-up",
        "vitals": ["bp", "weight", "bmi", "glucose"],
    },
}

STAGES = {
    "initial": "Initial",
    "reassessment": "Reassessment",
    "pre_procedure": "Pre-procedure",
    "post_procedure": "Post-procedure",
    "observation": "Under observation",
    "before_discharge": "Before discharge",
}
DEFAULT_ASSESSMENT = "general_adult"
DEFAULT_STAGE = "initial"
# Several timed readings are expected here, so each is a new record instead of an edit.
REPEATING_STAGES = {"observation"}
NOTE_REQUIRED_STAGES = {"before_discharge"}

CORE = ["bp", "pulse", "resp_rate", "temp", "spo2"]
OBSERVATION = ["pulse", "bp", "spo2"]
KEY_AT_DISCHARGE = ["bp", "pulse", "temp", "spo2"]


def vitals_for(assessment, stage):
    """Ordered vital keys asked for this assessment type at this stage."""
    full = ASSESSMENTS[assessment]["vitals"]
    if stage in ("initial", "reassessment"):
        return list(full)
    if stage in ("pre_procedure", "post_procedure"):
        return [k for k in full if k in CORE] or list(full[:3])
    if stage == "observation":
        return list(OBSERVATION)
    if stage == "before_discharge":
        return [k for k in full if k in KEY_AT_DISCHARGE] or list(full[:3])
    return list(full)


def field(assessment, key):
    base = dict(VITALS[key])
    spec = ASSESSMENTS[assessment]
    base["key"] = key
    base["label"] = spec.get("labels", {}).get(key, base["label"])
    if key in spec.get("hints", {}):
        base["hint"] = spec["hints"][key]
    return base


def config():
    """Everything the nurse screen needs, in one reply."""
    return {
        "stages": [{"value": k, "label": v, "repeating": k in REPEATING_STAGES} for k, v in STAGES.items()],
        "assessments": [
            {
                "value": key,
                "label": spec["label"],
                "stages": {
                    stage: {
                        "fields": [field(key, k) for k in vitals_for(key, stage)],
                        "note_required": stage in NOTE_REQUIRED_STAGES,
                    }
                    for stage in STAGES
                },
            }
            for key, spec in ASSESSMENTS.items()
        ],
        "default_assessment": DEFAULT_ASSESSMENT,
        "default_stage": DEFAULT_STAGE,
    }


class VitalsError(ValueError):
    pass


def _number(spec, label, raw):
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise VitalsError(f"{label}: enter a number.")
    if value < Decimal(str(spec["min"])) or value > Decimal(str(spec["max"])):
        raise VitalsError(f"{label}: {value} looks wrong (expected {spec['min']} to {spec['max']} {spec['unit']}).")
    return value


def clean(assessment, stage, raw):
    """Check a nurse's readings against the rules for this assessment and stage; return the stored dict.

    Empty values are dropped. BMI is worked out from weight and height when both are present.
    """
    if assessment not in ASSESSMENTS:
        raise VitalsError("Choose an assessment type.")
    if stage not in STAGES:
        raise VitalsError("Choose a stage.")
    if raw in (None, ""):
        raw = {}
    if not isinstance(raw, dict):
        raise VitalsError("Vitals must be a set of readings.")
    allowed = vitals_for(assessment, stage)
    out = {}
    for key, value in raw.items():
        if value in (None, ""):
            continue
        if key not in VITALS:
            raise VitalsError(f"{key} is not a vital sign.")
        if key not in allowed and not VITALS[key].get("computed"):
            raise VitalsError(f"{VITALS[key]['label']} is not recorded for {ASSESSMENTS[assessment]['label']} at this stage.")
        spec = VITALS[key]
        label = ASSESSMENTS[assessment].get("labels", {}).get(key, spec["label"])
        if spec["kind"] == BP:
            text = str(value).strip().replace(" ", "")
            match = re.fullmatch(r"(\d{2,3})/(\d{2,3})", text)
            if not match or int(match.group(1)) <= int(match.group(2)) or int(match.group(1)) > 300:
                raise VitalsError(f"{label}: write it as systolic/diastolic, for example 120/80.")
            out[key] = text
        elif spec["kind"] == CHOICE:
            if str(value) not in spec["choices"]:
                raise VitalsError(f"{label}: choose one of {', '.join(spec['choices'])}.")
            out[key] = str(value)
        elif spec.get("computed"):
            continue  # recalculated below
        else:
            number = _number(spec, label, value)
            out[key] = format(number.normalize(), "f")
    if "weight" in out and "height" in out and "bmi" in allowed:
        metres = Decimal(out["height"]) / 100
        out["bmi"] = format((Decimal(out["weight"]) / (metres * metres)).quantize(Decimal("0.1")), "f")
    return out


def display(assessment, vitals):
    """Readings as [{key, label, unit, value}] in a stable order, for any screen or printout."""
    spec = ASSESSMENTS.get(assessment) or ASSESSMENTS[DEFAULT_ASSESSMENT]
    order = list(VITALS)
    rows = []
    for key in sorted((vitals or {}), key=lambda k: order.index(k) if k in order else 99):
        meta = VITALS.get(key, {"label": key.replace("_", " ").capitalize(), "unit": ""})
        rows.append(
            {
                "key": key,
                "label": spec.get("labels", {}).get(key, meta["label"]),
                "unit": meta.get("unit", ""),
                "value": str((vitals or {})[key]),
            }
        )
    return rows
