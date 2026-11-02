# Engine prompt E-GC · `aclose_async_compiled_graph()` hangs in prismal-ai 3.20.0

Found by the host while adopting 3.20 (H-320, [HOST-PROMPT-engine-3.20.md](./HOST-PROMPT-engine-3.20.md)).
The host works around it; this prompt is the engine fix.

```
You are working in the prismal engine repo (PyPI dist prismal-ai, module prismal).
Read prismal/agents/graph.py: _async_graph, _checkpointer_conns (a weakref.WeakSet),
_checkpointer_cms, build_checkpointer() and aclose_async_compiled_graph() (added in
commit 23a35c0, released in 3.20.0).

Bug: aclose_async_compiled_graph() can hang forever, so a process that calls it at
shutdown still never exits.

Mechanism (reproduced in prismal-server with faulthandler + instrumented
Connection.close/stop):
1. The function sets `_async_graph = None` FIRST.
2. When the caller holds no other reference to the graph, that drops the last strong
   ref to the graph -> its AsyncSqliteSaver -> the aiosqlite Connection.
   _checkpointer_conns is a WeakSet, so it does not keep the connection alive.
3. The connection is garbage-collected. aiosqlite's Connection.__del__ calls stop(),
   which tells the worker thread to exit without resolving pending futures.
4. Either the WeakSet is already empty (nothing gets closed, the thread may linger)
   or `await conn.close()` runs on a connection whose worker has stopped: the
   close future is never resolved and the await never returns.

Typical trigger: a host that calls get_async_compiled_graph() inside a request or a
factory and does not keep the result (prismal-server's default builder before its
workaround). Engine tests pass because the test still holds the graph.

Fix (TDD: failing test, then minimal code, then green):
1. In aclose_async_compiled_graph(), take strong references before dropping the
   graph: `conns = list(_checkpointer_conns)` (and the cms) as the FIRST statement,
   close them, and only then set `_async_graph = None`. Keep the existing
   per-connection try/except + warning log and idempotency.
2. Consider making _checkpointer_conns a plain set (connections are explicitly
   closed and discarded; the weak set only hides them from the closer). If kept
   weak, document why.
3. Regression test: build the graph with a sqlite checkpointer, DROP every reference
   the test holds (`del graph; gc.collect()` is not required to reproduce, but
   add it), then `await asyncio.wait_for(aclose_async_compiled_graph(), 5)`; assert
   it returns and that no non-daemon aiosqlite thread is alive afterwards
   (`[t for t in threading.enumerate() if not t.daemon and t is not
   threading.main_thread()] == []`). It must FAIL (timeout) on 3.20.0.
4. A subprocess test: `python -c "<get graph without keeping it; close; print ok>"`
   with a 60 s timeout, asserting returncode 0.
5. Release as 3.20.1 (patch). CHANGELOG -> Fixed.

Acceptance (EARS):
- WHEN aclose_async_compiled_graph() is awaited and no caller holds the graph THE
  SYSTEM SHALL close every sqlite/postgres checkpointer and return.
- WHEN it returns THE SYSTEM SHALL leave no non-daemon checkpointer thread running.

Host follow-up once 3.20.1 ships: raise the floor to prismal-ai>=3.20.1,<4 and remove
prismal_server.deps.ENGINE_GRAPHS / hold_engine_graph (the workaround) and its unit
test test_engine_graph_held_alive_during_close; the contract test
tests/contract/test_process_exit.py stays as the guard.
```
