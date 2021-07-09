from asyncio import Future, InvalidStateError, sleep
from contextlib import suppress
from locale import strxfrm
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Sequence, Tuple
from uuid import UUID, uuid4

from pynvim_pp.lib import async_call
from std2.pickle import new_decoder

from ...shared.parse import lower
from ...shared.runtime import Supervisor
from ...shared.runtime import Worker as BaseWorker
from ...shared.settings import BaseClient
from ...shared.types import Completion, Context, Edit, NvimPos
from .types import Msg

_LUA = (Path(__file__).resolve().parent / "request.lua").read_text("UTF-8")

_DECODER = new_decoder(Msg)


class Worker(BaseWorker[BaseClient, None]):
    def __init__(self, supervisor: Supervisor, options: BaseClient, misc: None) -> None:
        self._cur: Tuple[UUID, Future] = uuid4(), Future()
        supervisor.nvim.api.exec_lua(_LUA, ())
        super().__init__(supervisor, options=options, misc=misc)

    async def _req(self, pos: NvimPos) -> Optional[Any]:
        _, fut = self._cur
        fut.cancel()
        await sleep(0)
        self._cur = token, fut = uuid4(), Future()

        def cont() -> None:
            args = (str(token), pos)
            self._supervisor.nvim.api.exec_lua("COQts_req(...)", args)

        await async_call(self._supervisor.nvim, cont)

        ret = await fut
        return ret

    async def notify(self, token: UUID, msg: Sequence[Any]) -> None:
        c_token, fut = self._cur
        if token == c_token:
            reply, *_ = msg
            with suppress(InvalidStateError):
                fut.set_result(reply)

    async def work(self, context: Context) -> AsyncIterator[Completion]:
        match = lower(context.words or context.syms)
        reply = self._req(context.position)
        resp: Msg = _DECODER(reply or ())
        for payload in resp:
            ltext = lower(payload.text)
            if ltext.startswith(match) and (len(payload.text) > len(match)):
                edit = Edit(new_text=payload.text)
                cmp = Completion(
                    source=self._options.short_name,
                    tie_breaker=self._options.tie_breaker,
                    label=edit.new_text.strip(),
                    sort_by=strxfrm(ltext),
                    primary_edit=edit,
                )
                yield cmp

