"""Common application questions: the things most job applications ask, answered once
up front (About Me page or Telegram /setup) so applications don't stop to ask.

Each answer is a normal About Me fact (section + label), so the form filler uses it
like any other fact.
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Fact
from app.profile import normalize_key

YES_NO = ["Yes", "No"]
DECLINE = "I don't wish to answer"


@dataclass(frozen=True)
class CommonQ:
    id: str
    section: str
    label: str
    group: str
    options: list[str] = field(default_factory=list)
    help: str = ""
    suggested: str = ""  # pre-selected in the form; saved only when the owner saves


COMMON: list[CommonQ] = [
    # Contact
    CommonQ("street", "contact", "Street address", "Contact"),
    CommonQ("city", "contact", "City", "Contact"),
    CommonQ("state", "contact", "State", "Contact"),
    CommonQ("zip", "contact", "ZIP code", "Contact"),
    CommonQ("country", "contact", "Country", "Contact", suggested="United States"),
    CommonQ("preferred_name", "contact", "Preferred name", "Contact",
            help="Leave as your first name if none"),
    CommonQ("linkedin", "contact", "LinkedIn URL", "Contact"),
    # Work eligibility
    CommonQ("authorized", "authorization", "Legally authorized to work in the United States", "Eligibility",
            YES_NO),
    CommonQ("sponsorship", "authorization", "Will require visa sponsorship now or in the future",
            "Eligibility",
            YES_NO),
    CommonQ("age18", "authorization", "At least 18 years old", "Eligibility", YES_NO),
    CommonQ("license", "authorization", "Valid driver's license", "Eligibility", YES_NO),
    CommonQ("background", "authorization", "Willing to undergo a background check", "Eligibility", YES_NO),
    CommonQ("drug", "authorization", "Willing to take a drug screen", "Eligibility", YES_NO),
    CommonQ("clearance", "authorization", "Security clearance", "Eligibility",
            help='e.g. "None" or "Secret (active)"', suggested="None"),
    CommonQ("worked_here", "answers", "Previously employed by the company I'm applying to", "Eligibility",
            YES_NO, help="Your usual answer; you'll be asked if a specific company is different",
            suggested="No"),
    CommonQ("relatives", "answers", "Relatives working at the company I'm applying to", "Eligibility",
            YES_NO, suggested="No"),
    CommonQ("noncompete", "answers", "Bound by a non-compete agreement", "Eligibility", YES_NO,
            suggested="No"),
    # Preferences
    CommonQ("salary", "preferences", "Desired salary", "Job preferences",
            help='e.g. "$70,000" or "Negotiable"'),
    CommonQ("start", "preferences", "Earliest start date", "Job preferences",
            help='e.g. "2 weeks after offer"'),
    CommonQ("relocate", "preferences", "Willing to relocate", "Job preferences", ["Yes", "No", "Depends"]),
    CommonQ("arrangement", "preferences", "Preferred work arrangement", "Job preferences",
            ["Remote", "Hybrid", "On-site", "Any"]),
    CommonQ("travel", "preferences", "Willing to travel", "Job preferences", help='e.g. "Up to 25%"'),
    CommonQ("shifts", "preferences", "Willing to work nights, weekends, or on-call", "Job preferences",
            YES_NO),
    CommonQ("hear", "preferences", "How did you hear about this job", "Job preferences",
            help='Default answer, e.g. "Company website"', suggested="Company website"),
    CommonQ("education_level", "education", "Highest level of education completed", "Background",
            ["High school or GED", "Some college", "Associate degree", "Bachelor's degree",
             "Master's degree", "Doctorate"]),
    CommonQ("years_it", "skills", "Years of IT experience", "Background"),
    CommonQ("languages", "skills", "Languages spoken", "Background", suggested="English"),
    # Voluntary self-identification (EEO). Optional; "don't wish to answer" is always fine.
    CommonQ("gender", "answers", "Gender (voluntary)", "Voluntary self-identification",
            ["Male", "Female", "Non-binary", DECLINE], suggested=DECLINE),
    CommonQ("hispanic", "answers", "Hispanic or Latino (voluntary)", "Voluntary self-identification",
            ["Yes", "No", DECLINE], suggested=DECLINE),
    CommonQ("race", "answers", "Race (voluntary)", "Voluntary self-identification",
            ["American Indian or Alaska Native", "Asian", "Black or African American",
             "Native Hawaiian or Other Pacific Islander", "White", "Two or more races", DECLINE],
            suggested=DECLINE),
    CommonQ("veteran", "answers", "Protected veteran status (voluntary)", "Voluntary self-identification",
            ["I am a protected veteran", "I am not a protected veteran", DECLINE], suggested=DECLINE),
    CommonQ("disability", "answers", "Disability status (voluntary)", "Voluntary self-identification",
            ["Yes, I have a disability", "No, I do not have a disability", DECLINE], suggested=DECLINE),
]

BY_ID = {q.id: q for q in COMMON}


def find(db: Session, q: CommonQ) -> Fact | None:
    return db.scalar(select(Fact).where(Fact.section == q.section, Fact.key == normalize_key(q.label)))


def missing(db: Session) -> list[CommonQ]:
    out = []
    for q in COMMON:
        f = find(db, q)
        if f is None or not (f.value or "").strip():
            out.append(q)
    return out
