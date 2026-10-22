"""Publish in workspace version order; retain IDs when acknowledgements are lost."""
class OutboxPublisher:
    def __init__(self, storage, publisher):
        self.storage, self.publisher = storage, publisher

    async def run_once(self):
        async with self.storage.transaction() as tx:
            pending = [record for record in await tx.all('outbox') if not record['published']]
        if not pending:
            return False
        record = min(pending, key=lambda item: (item['event']['resources']['workspace_id'], item['event']['resource_version']))
        await self.publisher.publish(record['event'])
        async with self.storage.transaction() as tx:
            record['published'] = True
            await tx.put('outbox', record['event']['event_id'], record)
        return True
