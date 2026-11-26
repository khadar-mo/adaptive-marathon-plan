"""Multidimensional fitness state assembled within one SQLite read transaction."""
import context_data
import fitness_recovery as recovery
import workload
import workload_inputs as wi
import workload_processing


DEFAULT_CONFIG = {"baseline_days": 28, "minimum_days": 14, "robust_deviations": [],
                  "expected_metrics": context_data.DEFAULT_SETTINGS["expected_wellness_metrics"]}


def assemble(db, archive, event, knowledge, mode, *, processing_inventory=None):
    workload_bundle = workload.collect(db, event, knowledge, mode, None, processing_inventory=processing_inventory)
    training = workload.calculate(workload_bundle)
    records = recovery.records_at(db, event, knowledge)
    return {"event_cutoff": event, "knowledge_cutoff": knowledge, "time_basis": mode,
            "exposure": training, "recovery": recovery.calculate(records, event, DEFAULT_CONFIG)}


def read(store, *, as_of=None, knowledge_cutoff=None, mode="EVENT_TIME_RECONSTRUCTION"):
    event, knowledge, mode, _, _ = workload.parameters(as_of=as_of, knowledge_cutoff=knowledge_cutoff, mode=mode)
    with store.history() as db:
        inventory = None
        if mode == "EVENT_TIME_RECONSTRUCTION" and knowledge_cutoff is None:
            inventory = workload_processing.observe(db)
            knowledge = inventory["known_at"]
        return assemble(db, store.archive, event, knowledge, mode, processing_inventory=inventory)
