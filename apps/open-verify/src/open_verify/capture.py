"""Paced evidence checkpoints, with temporary non-interactive click highlights."""

import contextlib


async def highlight(page, target):
    await target.scroll_into_view_if_needed(timeout=5000)
    box = await target.bounding_box(timeout=5000)
    if box is None:
        raise ValueError("Click target has no visible bounding box")
    return await page.evaluate_handle("""box => {
        const marker = document.createElement('div');
        marker.dataset.ovClickHighlight = 'true';
        Object.assign(marker.style, {
            position: 'fixed', left: `${box.x - 4}px`, top: `${box.y - 4}px`,
            width: `${box.width + 8}px`, height: `${box.height + 8}px`,
            border: '3px solid #f97316', borderRadius: '6px', boxSizing: 'border-box',
            pointerEvents: 'none', zIndex: '2147483647',
        });
        const label = document.createElement('span');
        label.textContent = 'Next click';
        Object.assign(label.style, {
            position: 'absolute', left: '0', top: box.y >= 30 ? '-27px' : '100%',
            background: '#9a3412', color: 'white', font: 'bold 14px sans-serif',
            padding: '3px 7px', whiteSpace: 'nowrap', borderRadius: '3px',
        });
        marker.appendChild(label);
        document.documentElement.appendChild(marker);
        return marker;
    }""", box)


async def remove_highlight(marker):
    if marker is not None:
        with contextlib.suppress(Exception):
            await marker.evaluate('(element) => element.remove()')
            await marker.dispose()


class CheckpointCapture:
    def __init__(self, directory):
        self.directory = directory
        self.paths = []
        self.omissions = []

    async def __call__(self, page, name, target=None):
        marker = None
        try:
            self.directory.mkdir(exist_ok=True)
            if target is not None:
                marker = await highlight(page, target)
            path = self.directory / f'checkpoint-{len(self.paths):03d}-{name}.png'
            await page.screenshot(path=str(path), timeout=5000, animations='disabled',
                mask=[page.locator('input[type="password"], input[autocomplete="one-time-code"]')])
            self.paths.append(path)
        except Exception as exc:
            self.omissions.append(f'Screenshot {name} omitted ({type(exc).__name__}).')
        finally:
            await remove_highlight(marker)
