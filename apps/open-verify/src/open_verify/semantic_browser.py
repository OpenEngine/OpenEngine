"""Engine-issued browser references and bounded diffs from Chromium accessibility data."""

import contextlib
import hashlib
import json
from difflib import SequenceMatcher
from uuid import uuid4

from open_verify.engine import ActionError

MAX_CONTROLS = 120
MAX_NODE_TEXT = 32_000
MAX_AX_NODES = 4_000
MAX_DIFF_ITEMS = 30
CLICK_ROLES = frozenset({'button', 'link', 'checkbox', 'radio', 'tab', 'menuitem',
    'menuitemcheckbox', 'menuitemradio', 'option', 'switch', 'combobox', 'textbox',
    'searchbox', 'spinbutton', 'slider', 'listbox', 'treeitem'})
STATE_NAMES = frozenset({'disabled', 'checked', 'expanded', 'selected', 'pressed',
                        'readonly', 'required', 'focused', 'editable', 'multiselectable'})


def fingerprint(value):
    """Canonical semantics exclude transient node/reference IDs when used by replay."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def screen_diff(previous, current, *, reset):
    """Bound both node and text deltas; the current full snapshot remains authoritative."""
    before = {} if reset or previous is None else {n['id']: n for n in previous['nodes']}
    after = {n['id']: n for n in current['nodes']}
    added = [n for key, n in after.items() if key not in before]
    removed = [n for key, n in before.items() if key not in after]
    changed = [{'id': key, 'before': before[key], 'after': node}
               for key, node in after.items() if key in before and before[key] != node]
    old_text = [] if reset or previous is None else previous['snapshot'].splitlines()
    new_text = current['snapshot'].splitlines()
    url_change = ({'before': previous['url'], 'after': current['url']}
                  if previous is not None and not reset and previous['url'] != current['url'] else None)
    text_changes = []
    for tag, i, j, a, b in SequenceMatcher(a=old_text, b=new_text, autojunk=False).get_opcodes():
        if tag != 'equal':
            text_changes.append({'kind': tag, 'before': old_text[i:j][:MAX_DIFF_ITEMS],
                                 'after': new_text[a:b][:MAX_DIFF_ITEMS],
                                 'truncated': j - i > MAX_DIFF_ITEMS or b - a > MAX_DIFF_ITEMS})
    return {'from': None if reset or previous is None else previous['observation_id'],
            'to': current['observation_id'], 'reset': reset,
            'unchanged': not reset and not (added or removed or changed or text_changes or url_change),
            'url': url_change,
            'added': added[:MAX_DIFF_ITEMS], 'removed': removed[:MAX_DIFF_ITEMS],
            'changed': changed[:MAX_DIFF_ITEMS], 'text': text_changes[:MAX_DIFF_ITEMS],
            'counts': {'added': len(added), 'removed': len(removed), 'changed': len(changed),
                       'text': len(text_changes)},
            'truncated': any(len(items) > MAX_DIFF_ITEMS for items in (added, removed, changed, text_changes))
                or any(c['truncated'] for c in text_changes) or current['nodes_truncated']}


class SemanticBrowser:
    """Keep references within one engine context; bind actions to actual elements."""

    def __init__(self):
        self.session = None
        self.document = None
        self.identity = uuid4().hex
        self.sequence = 0
        self.next_id = 1
        self.ids = {}
        self.targets = {}
        self.latest = None
        self.signature = None

    async def observe(self, page, snapshot, *, truncated):
        """Project current controls and retire references when the document/screen changes."""
        if self.session is None:
            self.session = await page.context.new_cdp_session(page)
        raw = (await self.session.send('Accessibility.getFullAXTree'))['nodes']
        root = next((n for n in raw if n.get('role', {}).get('value') == 'RootWebArea'), None)
        if root is None:
            raise ValueError('Accessibility root is unavailable')
        document = root.get('backendDOMNodeId')
        if not isinstance(document, int):
            raise ValueError('Accessibility document identity is unavailable')
        reset = self.latest is None or self.document != document
        if reset:
            self.ids.clear()
        self.document = document
        all_nodes = {n['nodeId']: n for n in raw}
        nodes, targets = [], {}
        incomplete = truncated or len(raw) > MAX_AX_NODES
        for item in raw[:MAX_AX_NODES]:
            role = item.get('role', {}).get('value', '')
            backend = item.get('backendDOMNodeId')
            if item.get('ignored') or backend is None:
                continue
            props = {p['name']: p.get('value', {}).get('value') for p in item.get('properties', [])}
            editable = props.get('editable') in {'plaintext', 'richtext'}
            if role not in CLICK_ROLES and not editable:
                continue
            name = item.get('name', {}).get('value', '')
            if not isinstance(name, str):
                name = ''
            if len(name) > 512:
                incomplete = True
            node_id = self.ids.get(backend)
            if node_id is None:
                node_id = f'n{self.next_id}'
                self.next_id += 1
                self.ids[backend] = node_id
            actions = ['click', 'press']
            if editable or role in {'textbox', 'searchbox'}:
                actions.append('fill')
            context, parent = [], item.get('parentId')
            seen = set()
            while parent in all_nodes and parent not in seen:
                seen.add(parent)
                ancestor = all_nodes[parent]
                parent_role = ancestor.get('role', {}).get('value', '')
                parent_name = str(ancestor.get('name', {}).get('value', ''))
                if not ancestor.get('ignored') and parent_role not in {'generic', 'none', 'RootWebArea'}:
                    context.append({'role': parent_role, 'name': parent_name[:160]})
                parent = ancestor.get('parentId')
            node = {'id': node_id, 'role': role, 'name': name[:512],
                    'states': {k: v for k, v in props.items() if k in STATE_NAMES},
                    'actions': actions, 'context': list(reversed(context[:3])), 'order': len(nodes)}
            nodes.append(node)
            targets[node_id] = backend
            if len(nodes) > MAX_CONTROLS:
                incomplete = True
                break
        if len(json.dumps(nodes)) > MAX_NODE_TEXT:
            incomplete = True
        if incomplete:
            nodes, targets = [], {}
        # A backend ID is physical identity within a document; removed elements
        # must never regain their former reference through a locator fallback.
        self.ids = {backend: node_id for node_id, backend in targets.items()}
        semantic = [{k: v for k, v in n.items() if k != 'id'} for n in nodes]
        signature = fingerprint({'document': document, 'url': page.url, 'snapshot': snapshot,
                                 'nodes': nodes, 'incomplete': incomplete})
        if self.signature != signature:
            self.sequence += 1
        current = {'observation_id': f'{self.identity}:{self.sequence}', 'nodes': nodes,
                   'snapshot': snapshot, 'url': page.url, 'nodes_truncated': incomplete}
        diff = screen_diff(self.latest, current, reset=reset)
        self.latest, self.targets, self.signature = current, targets, signature
        return {'observation_id': current['observation_id'], 'nodes': nodes,
                'nodes_truncated': incomplete, 'semantic_version': 1,
                'semantic_fingerprint': fingerprint(semantic) if not incomplete else None,
                'screen_diff': diff}

    def invalidate(self):
        """An unavailable observation cannot authorize any previously issued reference."""
        self.latest = None
        self.targets.clear()
        self.signature = None

    def require(self, observation_id, node_id, operation):
        """Reject stale, unknown or unsupported references without resolving a selector."""
        if self.latest is None or observation_id != self.latest['observation_id']:
            raise ActionError('STALE_OBSERVATION', 'Observe the current screen before using a node reference')
        node = next((n for n in self.latest['nodes'] if n['id'] == node_id), None)
        if node is None or node_id not in self.targets:
            raise ActionError('NODE_NOT_FOUND', 'Node is not in the current observation')
        if operation not in node['actions'] or node['states'].get('disabled'):
            raise ActionError('NODE_NOT_ACTIONABLE', 'Node does not support this action in its current state')
        if operation == 'fill' and node['states'].get('readonly'):
            raise ActionError('NODE_NOT_ACTIONABLE', 'Node is read-only')
        return node

    async def bind(self, page, node_id):
        """Bridge a CDP backend node to a public Playwright ElementHandle without DOM attributes."""
        key = '__open_verify_' + uuid4().hex
        group = 'open-verify-' + uuid4().hex
        try:
            remote = (await self.session.send('DOM.resolveNode', {
                'backendNodeId': self.targets[node_id], 'objectGroup': group}))['object']
            await self.session.send('Runtime.callFunctionOn', {
                'objectId': remote['objectId'],
                'functionDeclaration': 'function(key) { Object.defineProperty(this.ownerDocument.defaultView, key, {value: this, configurable: true}); }',
                'arguments': [{'value': key}]})
            handle = await page.evaluate_handle('key => globalThis[key]', key)
            element = handle.as_element()
            if element is None:
                await handle.dispose()
                raise ValueError('Reference did not resolve to an element')
            if not await element.evaluate('(element) => element.isConnected'):
                await element.dispose()
                raise ValueError('Referenced element was detached')
            return element
        except Exception as exc:
            raise ActionError('NODE_RESOLUTION_FAILED', 'Referenced element is unavailable; observe again') from exc
        finally:
            with contextlib.suppress(Exception):
                await page.evaluate('key => delete globalThis[key]', key)
            with contextlib.suppress(Exception):
                await self.session.send('Runtime.releaseObjectGroup', {'objectGroup': group})

    async def close(self):
        """Release the observation CDP session with the owning browser context."""
        if self.session is not None:
            with contextlib.suppress(Exception):
                await self.session.detach()
        self.session = None
        self.invalidate()
