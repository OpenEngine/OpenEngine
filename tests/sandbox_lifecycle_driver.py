"""Crash-test child: hold a real ACP node/VM until the test sends SIGKILL."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys

from engine.adapters.sandbox.smolvm import SmolvmSandbox
from engine.graph_runtime import GraphId
from engine.graph_runtime_langgraph import ACPNode, LangGraphDefinition, LangGraphRuntime
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph_acp import ACPAgentRegistry, StdioACPProvider


async def main():
    image, executable, state, workspace, ready = sys.argv[1:]
    class Capture(SmolvmSandbox):
        instance = None
        @asynccontextmanager
        async def create(self, spec):
            async with super().create(spec) as vm:
                self.instance = vm
                yield vm
    backend = Capture(image, executable=executable, state_directory=state)
    builder = StateGraph(dict)
    provider = StdioACPProvider(name='test', command=('python3', 'agent.py', '--hang-prompt'))
    builder.add_node('agent', ACPNode(agent='test', registry=ACPAgentRegistry([provider]),
        cwd=workspace, prompt='Hold the VM', timeout=1200))
    builder.add_edge(START, 'agent')
    builder.add_edge('agent', END)
    runtime = LangGraphRuntime(LangGraphDefinition(graph_id=GraphId('crash'), name='Crash',
        graph=builder.compile(checkpointer=InMemorySaver())), sandbox=backend)
    try:
        run = await runtime.start(GraphId('crash'), {})
        async with asyncio.timeout(90):
            while True:
                vm = backend.instance
                if vm is not None:
                    process = await vm.exec(('python3', '-c', "from pathlib import Path; print(Path('node-ready').exists())"))
                    output = await process.stdout.read()
                    assert await process.wait() == 0
                    if output.strip() == b'True':
                        Path(ready).write_text(json.dumps({'name': vm.name, 'run': str(run.run_id)}))
                        break
                await asyncio.sleep(0.1)
        await asyncio.Future()
    finally:
        await runtime.aclose()


asyncio.run(main())
