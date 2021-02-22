# piker: trading gear for hackers
# Copyright (C) Tyler Goodlet (in stewardship for piker0)

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
Orders and execution client API.

"""
from contextlib import asynccontextmanager
from typing import Dict, Tuple
from pprint import pformat
from dataclasses import dataclass, field

import trio
import tractor

from ..data._source import Symbol
from ..log import get_logger
from ._ems import _ems_main


log = get_logger(__name__)


@dataclass
class OrderBook:
    """Buy-side (client-side ?) order book ctl and tracking.

    A style similar to "model-view" is used here where this api is
    provided as a supervised control for an EMS actor which does all the
    hard/fast work of talking to brokers/exchanges to conduct
    executions.

    Currently, this is mostly for keeping local state to match the EMS
    and use received events to trigger graphics updates.

    """
    # mem channels used to relay order requests to the EMS daemon
    _to_ems: trio.abc.SendChannel
    _from_order_book: trio.abc.ReceiveChannel

    _sent_orders: Dict[str, dict] = field(default_factory=dict)
    _ready_to_receive: trio.Event = trio.Event()

    def send(
        self,
        uuid: str,
        symbol: 'Symbol',
        price: float,
        size: float,
        action: str,
        exec_mode: str,
    ) -> dict:
        cmd = {
            'action': action,
            'price': price,
            'size': size,
            'symbol': symbol.key,
            'brokers': symbol.brokers,
            'oid': uuid,
            'exec_mode': exec_mode,  # dark or live
        }
        self._sent_orders[uuid] = cmd
        self._to_ems.send_nowait(cmd)
        return cmd

    async def modify(self, oid: str, price) -> bool:
        ...

    def cancel(self, uuid: str) -> bool:
        """Cancel an order (or alert) from the EMS.

        """
        cmd = self._sent_orders[uuid]
        msg = {
            'action': 'cancel',
            'oid': uuid,
            'symbol': cmd['symbol'],
        }
        self._to_ems.send_nowait(msg)


_orders: OrderBook = None


def get_orders(
    emsd_uid: Tuple[str, str] = None
) -> OrderBook:
    """"
    OrderBook singleton factory per actor.

    """
    if emsd_uid is not None:
        # TODO: read in target emsd's active book on startup
        pass

    global _orders

    if _orders is None:
        # setup local ui event streaming channels for request/resp
        # streamging with EMS daemon
        _orders = OrderBook(*trio.open_memory_channel(100))

    return _orders


# TODO: make this a ``tractor.msg.pub``
async def send_order_cmds():
    """Order streaming task: deliver orders transmitted from UI
    to downstream consumers.

    This is run in the UI actor (usually the one running Qt but could be
    any other client service code). This process simply delivers order
    messages to the above ``_to_ems`` send channel (from sync code using
    ``.send_nowait()``), these values are pulled from the channel here
    and relayed to any consumer(s) that called this function using
    a ``tractor`` portal.

    This effectively makes order messages look like they're being
    "pushed" from the parent to the EMS where local sync code is likely
    doing the pushing from some UI.

    """
    book = get_orders()
    orders_stream = book._from_order_book

    # signal that ems connection is up and ready
    book._ready_to_receive.set()

    async for cmd in orders_stream:

        # send msg over IPC / wire
        log.info(f'Send order cmd:\n{pformat(cmd)}')
        yield cmd


@asynccontextmanager
async def open_ems(
    broker: str,
    symbol: Symbol,
) -> None:
    """Spawn an EMS daemon and begin sending orders and receiving
    alerts.


    This EMS tries to reduce most broker's terrible order entry apis to
    a very simple protocol built on a few easy to grok and/or
    "rantsy" premises:

    - most users will prefer "dark mode" where orders are not submitted
      to a broker until and execution condition is triggered
      (aka client-side "hidden orders")

    - Brokers over-complicate their apis and generally speaking hire
      poor designers to create them. We're better off using creating a super
      minimal, schema-simple, request-event-stream protocol to unify all the
      existing piles of shit (and shocker, it'll probably just end up
      looking like a decent crypto exchange's api)

    - all order types can be implemented with client-side limit orders

    - we aren't reinventing a wheel in this case since none of these
      brokers are exposing FIX protocol; it is they doing the re-invention.


    TODO: make some fancy diagrams using mermaid.io

    the possible set of responses from the stream  is currently:
    - 'dark_submitted', 'broker_submitted'
    - 'dark_cancelled', 'broker_cancelled'
    - 'dark_executed', 'broker_executed'
    - 'broker_filled'
    """
    actor = tractor.current_actor()

    # TODO: add ``maybe_spawn_emsd()`` for this
    async with tractor.open_nursery() as n:

        portal = await n.start_actor(
            'emsd',
            enable_modules=[
                'piker.exchange._ems',
            ],
        )
        trades_stream = await portal.run(
            _ems_main,
            client_actor_name=actor.name,
            broker=broker,
            symbol=symbol.key,

        )

        # wait for service to connect back to us signalling
        # ready for order commands
        book = get_orders()

        with trio.fail_after(10):
            await book._ready_to_receive.wait()

        yield book, trades_stream
