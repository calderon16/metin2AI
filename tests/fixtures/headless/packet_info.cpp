// Test fixture'ı: sunucu tarafı (game/src/packet_info.cpp) boyut tablosu biçimi
CPacketInfoCG::CPacketInfoCG()
{
	Set(HEADER_CG_HANDSHAKE, sizeof(TPacketGCHandshake), "Handshake", false);
	Set(HEADER_CG_PONG, sizeof(TPacketCGHeader), "Pong", false);
	Set(HEADER_CG_LOGIN2, sizeof(TPacketCGLogin2), "Login2", false);
	Set(HEADER_CG_LOGIN3, sizeof(TPacketCGLogin3), "Login3", false);
	Set(HEADER_CG_ATTACK, sizeof(TPacketCGAttack), "Attack", true);
	Set(HEADER_CG_CHAT, sizeof(TPacketCGChat), "Chat", true);
	Set(HEADER_CG_CHARACTER_SELECT, sizeof(TPacketCGPlayerSelect), "Select", true);
	Set(HEADER_CG_ENTERGAME, sizeof(TPacketCGHeader), "EnterGame", true);
	Set(HEADER_CG_ITEM_USE, sizeof(TPacketCGItemUse), "ItemUse", true);
	Set(HEADER_CG_ITEM_PICKUP, sizeof(TPacketCGItemPickup), "ItemPickup", true);
	Set(HEADER_CG_ON_CLICK, sizeof(TPacketCGOnClick), "OnClick", false);
	Set(HEADER_CG_SCRIPT_ANSWER, sizeof(TPacketCGScriptAnswer), "ScriptAnswer", true);
	Set(HEADER_CG_TARGET, sizeof(TPacketCGTarget), "Target", true);
	Set(HEADER_CG_MOVE, sizeof(TPacketCGMove), "Move", true);
}
