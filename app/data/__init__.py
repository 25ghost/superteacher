"""Reference data for the SuperTeacher backend.

This package holds the shared education reference datasets (academic years,
pathways, education levels, pathway levels, subjects, programs, program
versions, TVET sectors/programs, schools, school offerings), the structural
validator, and the idempotent seed loader core.

Whole-system framing: shared SuperTeacher reference data, consumed first by
the Student Registration module and reused by future modules. It is NOT part
of a Student Registration service.
"""
from app.data._spec import EXPECTED_LOAD_ORDER, Dataset, DatasetSpec

__all__ = ["EXPECTED_LOAD_ORDER", "Dataset", "DatasetSpec"]
