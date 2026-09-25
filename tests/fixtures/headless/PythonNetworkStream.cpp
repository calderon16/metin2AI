// Test fixture'ı: client paket boyut tablosu (CMainPacketHeaderMap) biçimi
class CMainPacketHeaderMap : public CNetworkPacketHeaderMap
{
public:
	CMainPacketHeaderMap()
	{
		Set(HEADER_GC_HANDSHAKE, CNetworkPacketHeaderMap::TPacketType(sizeof(TPacketGCHandshake), STATIC_SIZE_PACKET));
		Set(HEADER_GC_PING, CNetworkPacketHeaderMap::TPacketType(sizeof(TPacketGCPing), STATIC_SIZE_PACKET));
		Set(HEADER_GC_PHASE, CNetworkPacketHeaderMap::TPacketType(sizeof(TPacketGCPhase), STATIC_SIZE_PACKET));
		Set(HEADER_GC_AUTH_SUCCESS, CNetworkPacketHeaderMap::TPacketType(sizeof(TPacketGCAuthSuccess), STATIC_SIZE_PACKET));
		Set(HEADER_GC_LOGIN_FAILURE, CNetworkPacketHeaderMap::TPacketType(sizeof(TPacketGCLoginFailure), STATIC_SIZE_PACKET));
		Set(HEADER_GC_LOGIN_SUCCESS4, CNetworkPacketHeaderMap::TPacketType(sizeof(TPacketGCLoginSuccess4), STATIC_SIZE_PACKET));
		Set(HEADER_GC_MAIN_CHARACTER, CNetworkPacketHeaderMap::TPacketType(sizeof(TPacketGCMainCharacter), STATIC_SIZE_PACKET));
		Set(HEADER_GC_CHARACTER_POINTS, CNetworkPacketHeaderMap::TPacketType(sizeof(TPacketGCPoints), STATIC_SIZE_PACKET));
		Set(HEADER_GC_CHARACTER_POINT_CHANGE, CNetworkPacketHeaderMap::TPacketType(sizeof(TPacketGCPointChange), STATIC_SIZE_PACKET));
		Set(HEADER_GC_CHARACTER_ADD, CNetworkPacketHeaderMap::TPacketType(sizeof(TPacketGCCharacterAdd), STATIC_SIZE_PACKET));
		Set(HEADER_GC_CHAR_ADDITIONAL_INFO, CNetworkPacketHeaderMap::TPacketType(sizeof(TPacketGCCharacterAdditionalInfo), STATIC_SIZE_PACKET));
		Set(HEADER_GC_CHARACTER_DEL, CNetworkPacketHeaderMap::TPacketType(sizeof(TPacketGCCharacterDelete), STATIC_SIZE_PACKET));
		Set(HEADER_GC_MOVE, CNetworkPacketHeaderMap::TPacketType(sizeof(TPacketGCMove), STATIC_SIZE_PACKET));
		Set(HEADER_GC_CHAT, CNetworkPacketHeaderMap::TPacketType(sizeof(TPacketGCChat), DYNAMIC_SIZE_PACKET));
		Set(HEADER_GC_DEAD, CNetworkPacketHeaderMap::TPacketType(sizeof(TPacketGCDead), STATIC_SIZE_PACKET));
		Set(HEADER_GC_ITEM_SET, CNetworkPacketHeaderMap::TPacketType(sizeof(TPacketGCItemSet), STATIC_SIZE_PACKET));
		Set(HEADER_GC_ITEM_GROUND_ADD, CNetworkPacketHeaderMap::TPacketType(sizeof(TPacketGCItemGroundAdd), STATIC_SIZE_PACKET));
		Set(HEADER_GC_ITEM_GROUND_DEL, CNetworkPacketHeaderMap::TPacketType(sizeof(TPacketGCItemGroundDel), STATIC_SIZE_PACKET));
		Set(HEADER_GC_SCRIPT, CNetworkPacketHeaderMap::TPacketType(sizeof(TPacketGCScript), DYNAMIC_SIZE_PACKET));
	}
};
