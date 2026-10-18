"""Generate the Logic Apps Standard project under logicapps/ (Phase 10).

    uv run python -m scripts.gen_logicapps

The workflow definitions are code here, JSON on disk: expressions are long and easy to break by
hand, and generating them keeps the two workflows' shared pieces (authentication, gateway calls)
identical. The output is committed; scripts/deploy_logicapps.sh zips and deploys it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1] / "logicapps"
SCHEMA = ("https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/"
          "workflowdefinition.json#")


def write(rel: str, obj: dict[str, Any]) -> None:
    path = ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(obj, indent=2) + "\n").encode("utf-8"))


def ok(action: str) -> str:
    return f"equals(actions('{action}')?['status'], 'Succeeded')"


HOST = {
    "version": "2.0",
    "extensionBundle": {"id": "Microsoft.Azure.Functions.ExtensionBundle.Workflows",
                        "version": "[1.*, 2.0.0)"},
}

# Every value comes from an app setting (logicapps.bicep). The APIM key is a Key Vault reference
# there, resolved by the platform with the workflow identity - never a value in this repo.
PARAMETERS = {
    "webCallbackUrl": {"type": "String", "value": "@appsetting('ATLAS_WEB_CALLBACK_URL')"},
    "webAudience": {"type": "String", "value": "@appsetting('ATLAS_WEB_AUDIENCE')"},
    "logicIdentityId": {"type": "String", "value": "@appsetting('ATLAS_LOGIC_IDENTITY_ID')"},
    "gatewayUrl": {"type": "String", "value": "@appsetting('ATLAS_GATEWAY_URL')"},
    "gatewayAudience": {"type": "String", "value": "@appsetting('ATLAS_GATEWAY_AUDIENCE')"},
    # String, not SecureString: Standard parameters.json supports only Array, Bool, Float, Int,
    # Object and String, and one invalid type makes the runtime register NO workflow at all
    # (API-NOTES 17.3). The key stays out of run history through secureData on the actions below.
    "apimKey": {"type": "String", "value": "@appsetting('ATLAS_APIM_KEY')"},
}

RUN_NAME = "@{workflow()?['run']?['name']}"

MSI_WEB = {"type": "ManagedServiceIdentity", "identity": "@parameters('logicIdentityId')",
           "audience": "@parameters('webAudience')"}
MSI_GATEWAY = {"type": "ManagedServiceIdentity", "identity": "@parameters('logicIdentityId')",
               "audience": "@parameters('gatewayAudience')"}


def approve_remediation() -> dict[str, Any]:
    """Collect a human decision durably. It never decides: the orchestrator does."""
    trigger_schema = {
        "type": "object", "required": ["case_id", "plan_id"],
        "properties": {
            "case_id": {"type": "string"}, "plan_id": {"type": "string"},
            "required_roles": {"type": "array", "items": {"type": "string"}},
            "expires_at": {"type": ["string", "null"]}, "timeout": {"type": "string"},
            "summary": {"type": "string"}, "console_url": {"type": "string"},
        },
    }
    notify = ("@{concat('Approval needed for ', triggerBody()?['case_id'], ' plan ', "
              "triggerBody()?['plan_id'], ' before ', triggerBody()?['expires_at'], ': ', "
              "triggerBody()?['summary'])}")
    return {
        "definition": {
            "$schema": SCHEMA, "contentVersion": "1.0.0.0",
            "triggers": {"When_an_approval_is_requested": {
                "type": "Request", "kind": "Http",
                "inputs": {"method": "POST", "schema": trigger_schema}}},
            "actions": {
                "Accepted": {
                    "type": "Response", "kind": "Http", "runAfter": {},
                    "inputs": {"statusCode": 202,
                               "headers": {"x-ms-workflow-run-id": RUN_NAME},
                               "body": {"case_id": "@{triggerBody()?['case_id']}",
                                        "run_id": RUN_NAME,
                                        "status": "waiting for a human"}}},
                "Notify_approvers": {
                    "type": "Compose", "runAfter": {"Accepted": ["Succeeded"]},
                    "inputs": {"channel": "log only (no mail or Teams connector in this tenant)",
                               "to_roles": "@triggerBody()?['required_roles']",
                               "message": notify}},
                "Post_console_link": {
                    "type": "Compose", "runAfter": {"Accepted": ["Succeeded"]},
                    "inputs": {"console_url": "@triggerBody()?['console_url']",
                               "note": "A person decides in the ATLAS console; the orchestrator "
                                       "checks it. This workflow only collects the answer."}},
                "Wait_for_decision": {
                    "type": "HttpWebhook",
                    "runAfter": {"Notify_approvers": ["Succeeded"],
                                 "Post_console_link": ["Succeeded"]},
                    "inputs": {
                        "subscribe": {
                            "method": "POST", "uri": "@parameters('webCallbackUrl')",
                            "body": {"action": "subscribe",
                                     "case_id": "@triggerBody()?['case_id']",
                                     "plan_id": "@triggerBody()?['plan_id']",
                                     "callback_url": "@listCallbackUrl()"},
                            "authentication": MSI_WEB},
                        "unsubscribe": {
                            "method": "POST", "uri": "@parameters('webCallbackUrl')",
                            "body": {"action": "unsubscribe",
                                     "case_id": "@triggerBody()?['case_id']",
                                     "plan_id": "@triggerBody()?['plan_id']"},
                            "authentication": MSI_WEB}},
                    # Whatever is left of the approval window, sent by the orchestrator.
                    "limit": {"timeout": "@{coalesce(triggerBody()?['timeout'], 'PT30M')}"}},
                "Record_decision": {
                    "type": "Compose", "runAfter": {"Wait_for_decision": ["Succeeded"]},
                    "inputs": {"decided_by_orchestrator": "@body('Wait_for_decision')"}},
                "Expiry_requested": {
                    # A handled timeout is the designed outcome, not a failure: end the run
                    # Succeeded so run history reads true (an unhandled TimedOut marks it Failed).
                    "type": "Terminate", "runAfter": {"Ask_orchestrator_to_expire": [
                        "Succeeded", "Failed"]},
                    "inputs": {"runStatus": "Succeeded"}},
                "Ask_orchestrator_to_expire": {
                    "type": "Http", "runAfter": {"Wait_for_decision": ["TimedOut"]},
                    "inputs": {"method": "POST", "uri": "@parameters('webCallbackUrl')",
                               "body": {"action": "expired",
                                        "case_id": "@triggerBody()?['case_id']",
                                        "plan_id": "@triggerBody()?['plan_id']"},
                               "authentication": MSI_WEB}},
            },
            "outputs": {},
        },
        "kind": "Stateful",
    }


def gateway_get(uri: str) -> dict[str, Any]:
    return {"type": "Http", "runAfter": {},
            "inputs": {"method": "GET", "uri": uri,
                       "headers": {"Ocp-Apim-Subscription-Key": "@parameters('apimKey')",
                                   "x-atlas-agent": "enrich-incident"},
                       "authentication": MSI_GATEWAY},
            "limit": {"timeout": "PT20S"},
            # The subscription key rides in the inputs: never let run history show it.
            "runtimeConfiguration": {"secureData": {"properties": ["inputs"]}}}


def enrich_incident() -> dict[str, Any]:
    """A whole workflow as one agent tool: fan out through the gateway, aggregate, answer."""
    sid = "triggerBody()?['service_id']"
    base = "@{parameters('gatewayUrl')}/ops"
    branches = ("Get_service", "Get_recent_changes", "Get_health", "Check_external_status_page")
    unavailable = (
        "@union("
        f"if({ok('Get_service')}, json('[]'), createArray('service')), "
        f"if({ok('Get_recent_changes')}, json('[]'), createArray('recent_changes')), "
        f"if({ok('Get_health')}, json('[]'), createArray('health')))")
    return {
        "definition": {
            "$schema": SCHEMA, "contentVersion": "1.0.0.0",
            "triggers": {"When_enrichment_is_requested": {
                "type": "Request", "kind": "Http",
                "inputs": {"method": "POST", "schema": {
                    "type": "object", "required": ["service_id"],
                    "properties": {"service_id": {"type": "string"},
                                   "window_minutes": {"type": "integer"}}}}}},
            "actions": {
                "Get_service": gateway_get(f"{base}/services/@{{{sid}}}"),
                "Get_recent_changes": gateway_get(
                    f"{base}/changes?service_id=@{{{sid}}}&since_minutes="
                    "@{coalesce(triggerBody()?['window_minutes'], 240)}"),
                "Get_health": gateway_get(
                    f"{base}/services/@{{{sid}}}/health?window_minutes="
                    "@{coalesce(triggerBody()?['window_minutes'], 60)}"),
                "Check_external_status_page": {
                    "type": "Compose", "runAfter": {},
                    "inputs": {"provider": "status.contoso.example (simulated)",
                               "status": "operational", "checked_at": "@{utcNow()}"}},
                "Aggregate": {
                    "type": "Compose",
                    "runAfter": {b: ["Succeeded", "Failed", "TimedOut", "Skipped"]
                                 for b in branches},
                    "inputs": {
                        "service_id": f"@{sid}",
                        "service": f"@if({ok('Get_service')}, body('Get_service'), null)",
                        "on_call_rotation": (f"@if({ok('Get_service')}, "
                                             "body('Get_service')?['on_call_rotation'], null)"),
                        "recent_changes": (f"@if({ok('Get_recent_changes')}, "
                                           "body('Get_recent_changes')?['changes'], null)"),
                        "health": f"@if({ok('Get_health')}, body('Get_health'), null)",
                        "external_status": "@outputs('Check_external_status_page')",
                        "unavailable": unavailable,
                        "enriched_at": "@{utcNow()}",
                        "source": "logic app enrich-incident, through the ATLAS gateway"}},
                "Response": {
                    "type": "Response", "kind": "Http", "runAfter": {"Aggregate": ["Succeeded"]},
                    "inputs": {"statusCode": 200,
                               "headers": {"Content-Type": "application/json"},
                               "body": "@outputs('Aggregate')"}},
            },
            "outputs": {},
        },
        "kind": "Stateful",
    }


def main() -> None:
    write("host.json", HOST)
    write("parameters.json", PARAMETERS)
    write("approve-remediation/workflow.json", approve_remediation())
    write("enrich-incident/workflow.json", enrich_incident())
    for path in sorted(ROOT.rglob("*.json")):
        print(path.relative_to(ROOT.parent))


if __name__ == "__main__":
    main()
