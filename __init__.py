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

    # ---- Editor helpers (syntax check / wildcard browser / preview) ----
    from . import parser
    from .nodes import _WILDCARDS_DIR

    _MAX_TEXT = 20000
    _MAX_EXPAND = 10

    async def _text_body(request):
        data = await request.json()
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str) or len(text) > _MAX_TEXT:
            raise web.HTTPBadRequest(text="text must be a string up to 20000 chars")
        return data, text

    @PromptServer.instance.routes.post("/sequencecraft/validate")
    async def validate(request):
        _, text = await _text_body(request)
        return web.json_response({"problems": parser.diagnose(text, _WILDCARDS_DIR)})

    @PromptServer.instance.routes.post("/sequencecraft/expand")
    async def expand(request):
        data, text = await _text_body(request)
        decl = data.get("declarations", "")
        seed, count = data.get("seed", 0), data.get("count", 1)
        if not isinstance(decl, str) or len(decl) > _MAX_TEXT or not isinstance(seed, int) \
                or not isinstance(count, int) or not 1 <= count <= _MAX_EXPAND:
            raise web.HTTPBadRequest(text="invalid declarations/seed/count")
        results = [parser.generate(decl + text, seed + i, _WILDCARDS_DIR) for i in range(count)]
        return web.json_response({"results": results})

    @PromptServer.instance.routes.get("/sequencecraft/wildcards")
    async def wildcard_list(request):
        return web.json_response({"names": parser.list_wildcards(_WILDCARDS_DIR)})

    @PromptServer.instance.routes.get("/sequencecraft/wildcard")
    async def wildcard_values(request):
        name = request.rel_url.query.get("name", "")
        if not parser.safe_wildcard_name(name):
            raise web.HTTPBadRequest(text="invalid name")
        values = parser.find_wildcard(name, _WILDCARDS_DIR)
        if not values:
            raise web.HTTPNotFound(text="no such wildcard")
        return web.json_response({"name": name, "values": values})

except Exception as e:
    print(f"[SequenceCraft] Warning: Could not register API routes: {e}")
