"""Read-only approval-shaped checks, not semantic accuracy or approval."""
from app.naming import prepare_payload, naming_errors, assignment_error
from app.ontology import validate_payload

def validate_for_review(kind, payload, evidence, snapshot):
    prepared = prepare_payload(kind, payload, evidence, snapshot)
    clean = validate_payload(kind, prepared, snapshot)
    issues = naming_errors(kind, clean)
    conflict = assignment_error(kind, clean, evidence, snapshot)
    if conflict:
        issues.append(conflict)
    if issues:
        raise ValueError('; '.join(issues))
    return clean
