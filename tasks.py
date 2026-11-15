import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="email_orchestrator.tasks.evaluate_decision", bind=True, max_retries=3, default_retry_delay=120)
def evaluate_decision(self, decision_id):
    from .engine import EngineTransient, decide
    try:
        row = decide(decision_id)
    except EngineTransient as exc:
        logger.warning("email decision %s transient failure: %s", decision_id, exc)
        raise self.retry(exc=exc)
    return {"decision": decision_id, "status": getattr(row, "status", None)}


@shared_task(name="email_orchestrator.tasks.send_decision", bind=True, max_retries=0)
def send_decision_task(self, decision_id):
    from .sending import perform_send
    row = perform_send(decision_id)
    return {"decision": decision_id, "status": getattr(row, "status", None)}
