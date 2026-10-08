"""Bound repository inspection and reuse evidence until application actions may change it."""

import json
from copy import deepcopy
from pathlib import PurePath

from open_verify.engine import READ_TOOLS


class InspectionBudget:
    """Keep discovery bounded and leave decisions for actual setup and verification."""

    def __init__(self, max_steps):
        self.discovery_limit = min(20, max(1, max_steps // 3))
        self.total_limit = min(30, max(1, max_steps // 2))
        self.reserve = min(10, max(1, max_steps // 6))
        self.discovery = self.total = 0
        self.cache = {}
        self.index = []

    def available(self, state, max_steps):
        """Inspection cannot spend the final execution reserve or exceed its phase limit."""
        return (self.total < self.total_limit
                and (state['stage'] != 'discover' or self.discovery < self.discovery_limit)
                and max_steps - state['steps'] > self.reserve)

    def context(self, state, max_steps):
        """Expose budgets and source identities across planner/case conversation resets."""
        return {'remaining_decisions': max(0, max_steps - state['steps']),
                'inspection_available': self.available(state, max_steps),
                'discovery_reads': self.discovery, 'discovery_limit': self.discovery_limit,
                'total_reads': self.total, 'total_limit': self.total_limit,
                'execution_reserve': self.reserve, 'inspected_sources': deepcopy(self.index)}

    def key(self, tool, arguments):
        """Normalize path/default spellings without accessing files outside the checked engine."""
        args = dict(arguments)
        args.pop('refresh', None)
        if tool in {'read_file', 'list_files'} and isinstance(args.get('path', '.'), str):
            args['path'] = str(PurePath(args.get('path', '.')))
        if tool == 'read_file':
            args.setdefault('offset', 0)
            args.setdefault('limit', 24000)
        if tool == 'read_change_diff':
            args.setdefault('offset', 0)
            args.setdefault('limit', 16000)
        return tool, json.dumps(args, sort_keys=True)

    def prior(self, tool, arguments):
        """An explicit refresh requests a new checked read after an external edit."""
        return None if arguments.get('refresh') else self.cache.get(self.key(tool, arguments))

    def remember(self, tool, arguments, receipt, stage):
        """Retain successful reads and missing-file evidence, including pagination hints."""
        self.total += 1
        self.discovery += stage == 'discover'
        self.cache[self.key(tool, arguments)] = deepcopy(receipt)
        result = receipt['result']
        self.index.append({'evidence': receipt['id'], 'tool': tool, 'arguments': deepcopy(arguments),
            'ok': receipt['ok'], 'truncated': result.get('truncated', False),
            'next_offset': result.get('next_offset'), 'error': result.get('error')})

    def invalidate(self, tool):
        """Setup/application actions may change files; previous read evidence stays indexed."""
        if tool not in READ_TOOLS:
            self.cache.clear()
