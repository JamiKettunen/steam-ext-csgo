from __future__ import annotations

import asyncio
import logging
import math
import struct
from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ... import utils
from ...errors import HTTPException
from ...game import CSGO, Game
from ...gateway import READ_U32
from ...models import EventParser, register
from ...protobufs import EMsg, GCMsg, GCMsgProto, MsgProto
from ...state import ConnectionState
from ...trade import Inventory
from .backpack import BackPack
from .enums import Language
from .models import AccountInfo, Sticker
from .protobufs import base_gcmessages as cso, cstrike15_gcmessages as cstrike, econ_gcmessages, gcsdk_gcmessages as so

if TYPE_CHECKING:
    from steam.protobufs.steammessages_clientserver_2 import CMsgGcClient

    from .client import Client

log = logging.getLogger(__name__)
READ_F32 = struct.Struct("<f").unpack_from


class GCState(ConnectionState):
    gc_parsers: dict[Language, EventParser]
    client: Client

    def __init__(self, client: Client, **kwargs: Any):
        super().__init__(client, **kwargs)
        self._unpatched_inventory: Callable[[Game], Coroutine[None, None, Inventory]] = None  # type: ignore
        self.backpack: BackPack = None  # type: ignore
        self._connected = asyncio.Event()
        self._gc_connected = asyncio.Event()

    @register(EMsg.ClientFromGC)
    async def parse_gc_message(self, msg: MsgProto[CMsgGcClient]) -> None:
        if msg.body.appid != self.client.GAME.id:
            return

        try:
            language = Language(utils.clear_proto_bit(msg.body.msgtype))
        except ValueError:
            return log.info(
                f"Ignoring unknown msg type: {msg.body.msgtype} ({utils.clear_proto_bit(msg.body.msgtype)})"
            )

        try:
            msg = (
                GCMsgProto(language, msg.body.payload)
                if utils.is_proto(msg.body.msgtype)
                else GCMsg(language, msg.body.payload)
            )
        except Exception as exc:
            return log.error(f"Failed to deserialize message: {language!r}, {msg.body.payload!r}", exc_info=exc)
        else:
            log.debug(f"Socket has received GC message %r from the websocket.", msg)

        self.dispatch("gc_message_receive", msg)
        self.run_parser(language, msg)

    @register(Language.ClientWelcome)
    def parse_gc_client_connect(self, _) -> None:
        if not self._connected.is_set():
            self.dispatch("gc_connect")
            self._connected.set()

    @register(Language)  # .ClientGoodbye
    def parse_client_goodbye(self, _=None) -> None:
        self.dispatch("gc_disconnect")
        self._connected.clear()

    @register(Language.ClientWelcome)
    async def parse_gc_client_connect(self, msg: GCMsgProto[so.CMsgClientWelcome]) -> None:
        if msg.body.outofdate_subscribed_caches:
            for cache in msg.body.outofdate_subscribed_caches[0].objects:
                if cache.type_id == 1:
                    await self.update_backpack(*(cso.CsoEconItem().parse(item_data) for item_data in cache.object_data))
                else:
                    log.debug(f"Unknown item {cache!r} updated")
        if self._connected.is_set():
            self._gc_connected.set()
            self.dispatch("gc_ready")

    def patch_user_inventory(self, new_inventory: Inventory) -> None:
        async def inventory(_, game: Game) -> Inventory:
            if game != CSGO:
                return await self._unpatched_inventory(game)

            return new_inventory

        self.client.user.__class__.inventory = inventory

    async def update_backpack(self, *cso_items: cso.CsoEconItem, is_cache_subscribe: bool = False) -> BackPack:
        await self.client.wait_until_ready()

        backpack = self.backpack or BackPack(await self._unpatched_inventory(CSGO))
        item_ids = [item.asset_id for item in backpack]

        if not all(cso_item.id in item_ids for cso_item in cso_items):
            try:
                await backpack.update()
            except HTTPException:
                await asyncio.sleep(30)

            item_ids = [item.asset_id for item in backpack]

            if not all(cso_item.id in item_ids for cso_item in cso_items):
                await self.restart_csgo()
                await backpack.update()  # if the item still isn't here

        items = []
        for cso_item in cso_items:  # merge the two items
            item = utils.get(backpack, asset_id=cso_item.id)
            if item is None:
                continue  # the item has been removed (gc sometimes sends you items that you have crafted/deleted)
            for attribute_name in cso_item.__annotations__:
                setattr(item, attribute_name, getattr(cso_item, attribute_name))

            is_new = is_cache_subscribe and (cso_item.inventory >> 30) & 1
            item.position = 0 if is_new else cso_item.inventory & 0xFFFF

            # is the item contained in a casket?
            casket_id_low = utils.get(cso_item.attribute, def_index=272)
            casket_id_high = utils.get(cso_item.attribute, def_index=273)
            if casket_id_low and casket_id_high:
                item.casket_id = int(
                    f"{bin(READ_U32(casket_id_low.value_bytes)[0])[2:]}"
                    f"{bin(READ_U32(casket_id_high.value_bytes)[0])[2:]}",
                    2,
                )

            custom_name = utils.get(cso_item.attribute, def_index=111)
            if custom_name and not item.custom_name:
                item.custom_name = custom_name.value_bytes[2:].decode("utf-8")

            paint_index = utils.get(cso_item.attribute, def_index=6)
            if paint_index:
                item.paint_index = READ_F32(paint_index.value_bytes)[0]

            paint_seed = utils.get(cso_item.attribute, def_index=7)
            if paint_seed:
                item.paint_seed = math.floor(READ_F32(paint_seed.value_bytes)[0])

            paint_wear = utils.get(cso_item.attribute, def_index=8)
            if paint_wear:
                item.paint_wear = READ_F32(paint_wear.value_bytes)[0]

            tradable_after_date = utils.get(cso_item.attribute, def_index=75)
            if tradable_after_date:
                item.tradable_after = datetime.utcfromtimestamp(READ_U32(tradable_after_date.value_bytes)[0])

            item.stickers = []
            attrs = Sticker.get_attrs()
            for i in range(1, 6):
                sticker_id = utils.get(cso_item.attribute, def_index=113 + (i * 4))
                if sticker_id:
                    sticker = Sticker(slot=i, sticker_id=READ_U32(sticker_id.value_bytes)[0])

                    for idx, attr in enumerate(attrs):
                        attribute = utils.get(item.attribute, def_index=114 + (i * 4) + idx)
                        if attribute:
                            setattr(sticker, attribute, READ_F32(attribute.value_bytes)[0])

                    item.stickers.append(sticker)

            if item.def_index == 1201:  # storage unit
                item.casket_contained_item_count = 0
                item_count = utils.get(cso_item.attribute, def_index=270)
                if item_count:
                    item.casket_contained_item_count = READ_U32(item_count.value_bytes)[0]

            if not is_cache_subscribe:
                items.append(item)

        self.patch_user_inventory(backpack)
        self.backpack = backpack
        return backpack

    @register(Language.MatchmakingGC2ClientHello)
    def handle_matchmaking_client_hello(self, msg: GCMsgProto[cstrike.CMsgGccStrike15V2MatchmakingGc2ClientHello]):
        self.account_info = AccountInfo(msg.body)
        self.dispatch("account_info", self.account_info)

    @register(Language.MatchList)
    def handle_match_list(self, msg: GCMsgProto[cstrike.CMsgGccStrike15V2MatchList]):
        self.dispatch("match_list", msg.body.matches, msg.body)

    @register(Language.PlayersProfile)
    def handle_players_profile(self, msg: GCMsgProto[cstrike.CMsgGccStrike15V2PlayersProfile]):
        if not msg.body.account_profiles:
            return

        profile = msg.body.account_profiles[0]

        self.dispatch("players_profile", profile)

    @register(Language.Client2GCEconPreviewDataBlockResponse)
    def handle_client_preview_data_block_response(
        self, msg: GCMsgProto[cstrike.CMsgGccStrike15V2Client2GcEconPreviewDataBlockResponse]
    ):
        # decode the wear
        buffer = utils.StructIO()
        buffer.write_u32(msg.body.iteminfo.paintwear)
        item = utils.get(self.backpack, id=msg.body.iteminfo.itemid)
        item.paint_wear = buffer.read_f32()
        self.dispatch("inspect_item_info", item)

    @register(Language.ItemCustomizationNotification)
    def handle_item_customization_notification(
        self, msg: GCMsgProto[econ_gcmessages.CMsgGcItemCustomizationNotification]
    ):
        if not msg.body.item_id or not msg.body.request:
            return

        self.dispatch("item_customization_notification", msg.body.item_id, msg.body.request)

    @register(Language.SOCreate)
    async def handle_so_create(self, msg: GCMsgProto[so.CMsgSoSingleObject]):
        if msg.body.type_id != 1:
            return  # Not an item

        cso_item = cso.CsoEconItem().parse(msg.body.object_data)
        item = await self.update_backpack(cso_item)
        if item is None:  # protect from a broken item
            return log.info("Received an item that isn't our inventory %r", cso_item)
        self.dispatch("item_receive", item)

    @utils.call_once
    async def restart_csgo(self) -> None:
        await self.client.change_presence(game=Game(id=0))
        self.parse_client_goodbye()
        await self.client.change_presence(game=CSGO, games=self.client._original_games)
        await self._connected.wait()

    @register(Language.SOUpdate)
    async def handle_so_update(self, msg: GCMsgProto[so.CMsgSoSingleObject]):
        await self._handle_so_update(msg.body)

    @register(Language.SOUpdateMultiple)
    async def handle_so_update_multiple(self, msg: GCMsgProto[so.CMsgSoMultipleObjects]):
        for object in msg.body.objects_modified:
            await self._handle_so_update(object)

    async def _handle_so_update(self, object: so.CMsgSoSingleObject):
        if object.type_id != 1:
            return log.debug("Unknown item %r updated", object)

        cso_item = cso.CsoEconItem().parse(object.object_data)

        before = utils.get(self.backpack, asset_id=cso_item.id)
        if before is None:
            return log.info("Received an item that isn't our inventory %r", object)
        after = await self.update_backpack(cso_item)
        self.dispatch("item_update", before, after)

    @register(Language.SODestroy)
    def handle_so_destroy(self, msg: GCMsgProto[so.CMsgSoSingleObject]):
        if msg.body.type_id != 1 or not self.backpack:
            return

        deleted_item = cso.CsoEconItem().parse(msg.body.object_data)
        item = utils.get(self.backpack, asset_id=deleted_item.id)
        if item is None:
            return log.info("Received an item that isn't our inventory %r", deleted_item)
        for attribute_name in deleted_item.__annotations__:
            setattr(item, attribute_name, getattr(deleted_item, attribute_name))
        self.backpack.items.remove(item)  # type: ignore
        self.dispatch("item_remove", item)
