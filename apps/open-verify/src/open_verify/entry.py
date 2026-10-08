"""A typed LangGraph entry-preparation stage, separate from case execution."""

from typing import TypedDict

from open_verify.models import Case


def entry_evidence_error(entry, observations):
    """Require current host source/UI receipts, never an actor summary or invented citation."""
    indexed = {e['id']: e for e in observations}
    fields = {'read_file': 'text', 'read_change_diff': 'diff',
              'browser_snapshot': 'snapshot', 'http_request': 'body'}
    for citation in entry.evidence:
        receipt = indexed.get(citation)
        if not receipt or not receipt['ok'] or receipt['tool'] not in fields:
            return f'Entry evidence {citation} is not a successful source/UI receipt'
        result = receipt['result']
        if not isinstance(result.get(fields[receipt['tool']]), str) or not result[fields[receipt['tool']]].strip():
            return f'Entry evidence {citation} contains no inspected source/UI content'
        if receipt['tool'] == 'http_request' and not 200 <= result.get('status', 0) < 300:
            return f'Entry evidence {citation} is not a successful HTTP response'
    return None


class EntryState(TypedDict):
    case: Case
    required: bool
    result: dict


class EntryPreparation:
    """Validate cited entry knowledge, then independently check its live controls."""

    def __init__(self, journeys, artifacts):
        from langgraph.graph import END, START, StateGraph

        self.journeys, self.artifacts = journeys, artifacts
        graph = StateGraph(EntryState)
        graph.add_node('validate_entry_evidence', self.validate)
        graph.add_node('verify_entry_readiness', self.verify)
        graph.add_edge(START, 'validate_entry_evidence')
        graph.add_conditional_edges('validate_entry_evidence',
            lambda state: 'verify_entry_readiness' if state['result']['status'] == 'passed' else END)
        graph.add_edge('verify_entry_readiness', END)
        self.graph = graph.compile()

    async def validate(self, state):
        case = state['case']
        entry = case.journey.entry
        error = ('Manual browser QA requires journey.entry with cited route evidence and required controls'
                 if entry is None and state['required'] else
                 entry_evidence_error(entry, self.artifacts.observations) if entry else None)
        result = {'case_id': case.id, 'status': 'blocked' if error else 'passed',
                  'detail': error or 'Entry evidence validated', 'checks': []}
        if error:
            result['code'] = 'ENTRY_CONTRACT_REQUIRED' if entry is None else 'ENTRY_EVIDENCE_INVALID'
        if entry or state['required']:
            self.artifacts.record('entry_contract', {'case_id': case.id, 'url': case.journey.url},
                                  result, not error)
        return {'result': result}

    async def verify(self, state):
        return {'result': await self.journeys.check_readiness(state['case'])}

    async def run(self, case, *, required=False):
        """Return readiness evidence only; no product case or health verdict is generated here."""
        state = await self.graph.ainvoke({'case': case, 'required': required, 'result': {}})
        return state['result']
