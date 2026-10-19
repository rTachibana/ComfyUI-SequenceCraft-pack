from .nodes import SequenceCraftExtension, ImagePresetSequencePickerNode

WEB_DIRECTORY = "./js"


async def comfy_entrypoint() -> SequenceCraftExtension:
    return SequenceCraftExtension()


try:
    from aiohttp import web
    from server import PromptServer

    @PromptServer.instance.routes.post("/sequencecraft/reset")
    async def reset_counter(request):
        data = await request.json()
        node_id = str(data.get("node_id", "default"))
        ImagePresetSequencePickerNode._counters[node_id] = 0
        return web.json_response({"status": "ok", "counter": 0})

    @PromptServer.instance.routes.get("/sequencecraft/counter")
    async def get_counter(request):
        node_id = str(request.rel_url.query.get("node_id", "default"))
        counter = ImagePresetSequencePickerNode._counters.get(node_id, 0)
        return web.json_response({"counter": counter})

except Exception as e:
    print(f"[SequenceCraft] Warning: Could not register API routes: {e}")
