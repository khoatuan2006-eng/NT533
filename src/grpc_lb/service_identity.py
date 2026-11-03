"""Names shared by application RPCs, health probes and registry registration."""

RPC_SERVICE = "processing.v1.ProcessingService"


def health_name(service_id: str) -> str:
    return f"{RPC_SERVICE}/{service_id}"
