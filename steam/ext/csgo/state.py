"""Licensed under The MIT License (MIT) - Copyright (c) 2020-present James H-B. See LICENSE"""

from __future__ import annotations

import logging
import math
import struct
import sys
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ... import utils
from ...models import register
from ...protobufs import friends
from .._gc import GCState as GCState_
from .backpack import Backpack, Casket, CasketItem, Paint, Sticker
from .enums import ItemFlags, ItemOrigin, ItemQuality, Language
from .models import User
from .protobufs import base, cstrike, sdk

if TYPE_CHECKING:
    from .client import Client

log = logging.getLogger(__name__)


def READ_F32(bytes: bytes, *, _unpacker: Callable[[bytes], tuple[float]] = struct.Struct("<f").unpack_from) -> float:
    (f32,) = _unpacker(bytes)
    return f32


def READ_U32(bytes: bytes, *, _unpacker: Callable[[bytes], tuple[int]] = struct.Struct("<I").unpack_from) -> int:
    (u32,) = _unpacker(bytes)
    return u32


class GCState(GCState_):
    gc_parsers: dict[Language, Callable[..., Any]]
    client: Client
    Language: type[Language] = Language
    backpack: Backpack
    _users: dict[int, User]

    def __init__(self, client: Client, **kwargs: Any):
        super().__init__(client, **kwargs)
        self.casket_items: dict[int, CasketItem] = {}

    def _store_user(self, proto: friends.CMsgClientPersonaStateFriend) -> User:
        try:
            user = self._users[proto.friendid & 0xFFFFFFFF]  # type: ignore
        except KeyError:
            user = User(state=self, proto=proto)
            self._users[user.id] = user
        else:
            user._update(proto)
        return user

    @register(Language.ClientConnectionStatus)
    def parse_client_goodbye(self, msg: sdk.ConnectionStatus | None = None) -> None:
        if msg is None or msg.status == sdk.GcConnectionStatus.NoSession:
            self.dispatch("gc_disconnect")
            self._gc_connected.clear()
            self._gc_ready.clear()

    @register(Language.ClientWelcome)
    async def parse_gc_client_connect(self, msg: sdk.ClientWelcome) -> None:
        if msg.outofdate_subscribed_caches:
            for cache in msg.outofdate_subscribed_caches[0].objects:
                if cache.type_id == 1:
                    await self.update_backpack(*(base.Item().parse(item_data) for item_data in cache.object_data))
                else:
                    log.debug(f"Unknown item {cache!r} updated")
        if not self._gc_ready.is_set():
            self._gc_ready.set()
            self.dispatch("gc_ready")

    def set(self, name: str, value: Any) -> None:
        # would be nice if this was a macro
        locals = sys._getframe(1).f_locals
        if locals["is_casket_item"]:
            setattr(locals["gc_item"], name, value)
        else:
            setattr(locals["item"], name, value)

    async def update_backpack(self, *gc_items: base.Item, is_cache_subscribe: bool = False) -> Backpack:
        await self.client.wait_until_ready()

        backpack = self.backpack if self.backpack is not None else await self.fetch_backpack(Backpack)

        for gc_item in gc_items:  # merge the two items
            item = utils.get(backpack, id=gc_item.id)
            is_casket_item = False
            if item is None:
                # is the item contained in a casket?
                casket_id_low = utils.get(gc_item.attribute, def_index=272)
                casket_id_high = utils.get(gc_item.attribute, def_index=273)
                if not (casket_id_low and casket_id_high):
                    log.info("Received an item that isn't our inventory %r", gc_item)
                    continue  # the item has been removed (gc sometimes sends you items that you have deleted)
                is_casket_item = True
                gc_item = utils.update_class(gc_item, CasketItem())
                gc_item.casket_id = int(
                    f"{READ_U32(casket_id_high.value_bytes):032b}{READ_U32(casket_id_low.value_bytes):032b}",
                    base=2,
                )
            else:
                for attribute_name in gc_item.__annotations__:
                    setattr(item, attribute_name, getattr(gc_item, attribute_name))

            is_new = is_cache_subscribe and (gc_item.inventory >> 30) & 1
            self.set("position", 0 if is_new else gc_item.inventory & 0xFFFF)

            custom_name = utils.get(gc_item.attribute, def_index=111)
            if custom_name:
                self.set("custom_name", custom_name.value_bytes[2:].decode("utf-8"))

            paint_index = utils.get(gc_item.attribute, def_index=6)
            paint_seed = utils.get(gc_item.attribute, def_index=7)
            paint_wear = utils.get(gc_item.attribute, def_index=8)
            if any((paint_index, paint_seed, paint_wear)):
                paint = Paint()
                self.set("paint", paint)
            # type ignores as pyright thinks paint can be unbound
            if paint_index:
                paint.index = READ_F32(paint_index.value_bytes)  # type: ignore
            if paint_seed:
                paint.seed = math.floor(READ_F32(paint_seed.value_bytes))  # type: ignore
            if paint_wear:
                (paint.wear) = READ_F32(paint_wear.value_bytes)  # type: ignore

            tradable_after_date = utils.get(gc_item.attribute, def_index=75)
            if tradable_after_date:
                self.set("tradable_after", datetime.utcfromtimestamp(READ_U32(tradable_after_date.value_bytes)))

            stickers = []
            self.set("stickers", stickers)
            for i in range(4, 24, 4):
                sticker_id = utils.get(gc_item.attribute, def_index=113 + i)
                if sticker_id:
                    sticker = Sticker(slot=i, id=READ_U32(sticker_id.value_bytes))  # type: ignore

                    for idx, attr in enumerate(Sticker._decodeable_attrs):
                        attribute = utils.get(gc_item.attribute, def_index=114 + i + idx)
                        if attribute:
                            setattr(sticker, attr, READ_F32(attribute.value_bytes))

                    stickers.append(sticker)

            self.set("quality", ItemQuality.try_value(gc_item.quality))
            self.set("flags", ItemFlags.try_value(gc_item.flags))
            self.set("origin", ItemOrigin.try_value(gc_item.origin))

            if gc_item.def_index == 1201:  # storage unit
                assert item is not None
                orig_idx = backpack.items.index(item)
                item = utils.update_class(item, Casket.__new__(Casket))  # __class__ assignment doesn't work here
                assert isinstance(item, Casket)
                backpack.items[orig_idx] = item  # type: ignore  # typed as a Sequence not a list
                item_count = utils.get(gc_item.attribute, def_index=270)
                self.set("contained_item_count", READ_U32(item_count.value_bytes) if item_count is not None else 0)

            elif not isinstance(gc_item, CasketItem) and gc_item.id in self.casket_items:
                del self.casket_items[gc_item.id]

            elif isinstance(gc_item, CasketItem):
                self.casket_items[gc_item.id] = gc_item

        self.backpack = backpack
        return backpack

    @register(Language.MatchmakingGC2ClientHello)
    def handle_matchmaking_client_hello(self, msg: cstrike.MatchmakingClientHello):
        self.client.user._profile_info_msg = msg

    @register(Language.MatchList)
    def handle_match_list(self, msg: cstrike.MatchList):
        self.dispatch("match_list", msg.matches, msg)

    async def fetch_user_csgo_profile(self, user_id: int) -> cstrike.PlayersProfile:
        await self.ws.send_gc_message(
            cstrike.ClientRequestPlayersProfile( account_id=user_id, request_level=32)
        )
        return await self.gc_wait_for(
            cstrike.PlayersProfile, check=lambda msg: isinstance(msg, cstrike.PlayersProfile) and msg.account_profiles[0].account_id == user_id
        )

    @register(Language.SOCreate)
    async def handle_so_create(self, msg: sdk.SOCreate):
        if msg.type_id != 1:
            return  # Not an item

        cso_item = base.Item().parse(msg.object_data)
        self.backpack = await self.fetch_backpack(Backpack)  # refresh the backpack
        item = utils.get(self.backpack, id=cso_item.id)

        if item is None and not (
            utils.get(cso_item.attribute, def_index=272) and utils.get(cso_item.attribute, def_index=273)
        ):  # it's also not a casket item
            return log.info("Received an item that isn't our inventory %r", cso_item)

        if item is not None:
            self.backpack.items.append(item)  # type: ignore
        await self.update_backpack(cso_item)
        if isinstance(cso_item, CasketItem):
            return log.debug("Received a casket item %r", cso_item)

        self.dispatch("item_receive", item)

    @register(Language.SOUpdate)
    async def handle_so_update(self, msg: sdk.SOUpdate):
        await self._handle_so_update(msg)

    @register(Language.SOUpdateMultiple)
    async def handle_so_update_multiple(self, msg: sdk.MultipleObjects):
        for object in msg.objects_modified:
            await self._handle_so_update(object)

    async def _handle_so_update(self, object: sdk.SOCreate | sdk.SODestroy | sdk.SOUpdate | sdk.MultipleObjectsSingleObject):  # this should probably be a protocol
        if object.type_id != 1:
            return log.debug("Unknown item %r updated", object)

        cso_item = base.Item().parse(object.object_data)

        before = utils.get(self.backpack, id=cso_item.id)
        if before is None:
            return log.info("Received an item that isn't our inventory %r", cso_item)
        after = utils.get(await self.update_backpack(cso_item), id=cso_item.id)
        self.dispatch("item_update", before, after)

    @register(Language.SODestroy)
    def handle_so_destroy(self, msg: sdk.SODestroy):
        if msg.type_id != 1 or not self.backpack:
            return

        deleted_item = base.Item().parse(msg.object_data)
        item = utils.get(self.backpack, id=deleted_item.id)
        if item is None:
            return log.info("Received an item that isn't our inventory %r", deleted_item)
        for attribute_name in deleted_item.__annotations__:
            setattr(item, attribute_name, getattr(deleted_item, attribute_name))
        self.backpack.items.remove(item)  # type: ignore
        self.dispatch("item_remove", item)
