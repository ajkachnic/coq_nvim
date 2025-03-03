from contextlib import closing, suppress
from hashlib import md5
from os.path import normcase
from pathlib import Path, PurePath
from sqlite3 import Connection, OperationalError
from typing import AbstractSet, Iterator, Mapping, cast

from pynvim_pp.lib import encode

from ...shared.executor import SingleThreadExecutor
from ...shared.settings import MatchOptions
from ...shared.sql import BIGGEST_INT, init_db, like_esc
from ...tags.types import Tag, Tags
from ..types import Interruptible
from .sql import sql

_SCHEMA = "v5"

_NIL_TAG = Tag(
    language="",
    path="",
    line=0,
    kind="",
    name="",
    pattern=None,
    typeref=None,
    scope=None,
    scopeKind=None,
    access=None,
)


def _init(db_dir: Path, cwd: PurePath) -> Connection:
    ncwd = normcase(cwd)
    name = f"{md5(encode(ncwd)).hexdigest()}-{_SCHEMA}"
    db = (db_dir / name).with_suffix(".sqlite3")
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = Connection(str(db), isolation_level=None)
    init_db(conn)
    conn.executescript(sql("create", "pragma"))
    conn.executescript(sql("create", "tables"))
    return conn


class CTDB(Interruptible):
    def __init__(self, vars_dir: Path, cwd: PurePath) -> None:
        self._ex = SingleThreadExecutor()
        self._vars_dir = vars_dir / "clients" / "tags"
        self._conn: Connection = self._ex.ssubmit(
            lambda: _init(self._vars_dir, cwd=cwd)
        )

    async def swap(self, cwd: PurePath) -> None:
        def cont() -> None:
            self._conn.close()
            self._conn = _init(self._vars_dir, cwd=cwd)

        async with self._lock:
            await self._ex.submit(cont)

    async def paths(self) -> Mapping[str, float]:
        def cont() -> Mapping[str, float]:
            with self._conn, closing(self._conn.cursor()) as cursor:
                cursor.execute(sql("select", "files"), ())
                files = {row["filename"]: row["mtime"] for row in cursor.fetchall()}
                return files

        try:
            return await self._ex.submit(cont)
        except OperationalError:
            return {}

    async def reconciliate(self, dead: AbstractSet[str], new: Tags) -> None:
        def cont() -> None:
            with suppress(OperationalError):
                with self._conn, closing(self._conn.cursor()) as cursor:

                    def m1() -> Iterator[Mapping]:
                        for filename, (lang, mtime, _) in new.items():
                            yield {
                                "filename": filename,
                                "filetype": lang,
                                "mtime": mtime,
                            }

                    def m2() -> Iterator[Mapping]:
                        for _, _, tags in new.values():
                            for tag in tags:
                                yield {**_NIL_TAG, **tag}

                    cursor.executemany(
                        sql("delete", "file"),
                        ({"filename": f} for f in dead | new.keys()),
                    )
                    cursor.executemany(sql("insert", "file"), m1())
                    cursor.executemany(sql("insert", "tag"), m2())
                    cursor.execute("PRAGMA optimize", ())

        await self._ex.submit(cont)

    async def select(
        self,
        opts: MatchOptions,
        filename: str,
        line_num: int,
        word: str,
        sym: str,
        limitless: int,
    ) -> Iterator[Tag]:
        def cont() -> Iterator[Tag]:
            with self._conn, closing(self._conn.cursor()) as cursor:
                cursor.execute(
                    sql("select", "tags"),
                    {
                        "cut_off": opts.fuzzy_cutoff,
                        "look_ahead": opts.look_ahead,
                        "limit": BIGGEST_INT if limitless else opts.max_results,
                        "filename": filename,
                        "line_num": line_num,
                        "word": word,
                        "sym": sym,
                        "like_word": like_esc(word[: opts.exact_matches]),
                        "like_sym": like_esc(sym[: opts.exact_matches]),
                    },
                )
                rows = cursor.fetchall()
                return (cast(Tag, {**row}) for row in rows)

        async with self._interruption():
            try:
                return await self._ex.submit(cont)
            except OperationalError:
                return iter(())
