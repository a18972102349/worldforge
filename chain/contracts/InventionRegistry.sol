// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @notice Minimal, non-financial WorldForge v1 registration contract.
/// @dev The project ships a dependency-free local reference model for offline
/// tests. This source intentionally has no upgrade hooks, value transfer,
/// delegatecall, or external calls.
contract InventionRegistry {
    struct Invention {
        address registrant;
        bytes32 packHash;
        bytes32 licenseHash;
        uint64 registeredAtBlock;
        uint8 parentCount;
        bool deprecated;
    }

    error ZeroHash();
    error TooManyParents();
    error ParentsNotStrictlyAscending();
    error ParentNotFound(bytes32 parentId);
    error InvalidContentURI();
    error URITooLong();
    error ConflictingRegistration(bytes32 inventionId);
    error UnknownInvention(bytes32 inventionId);
    error NotRegistrant();

    mapping(bytes32 => Invention) private _inventions;
    mapping(bytes32 => bytes32[]) private _parents;
    mapping(bytes32 => string) private _contentURIs;
    mapping(bytes32 => bytes32) private _authorPack;
    mapping(bytes32 => bool) private _exists;

    event InventionRegistered(
        bytes32 indexed inventionId,
        address indexed registrant,
        bytes32 indexed packHash,
        bytes32[] parentIds,
        bytes32 licenseHash,
        string contentURI
    );
    event InventionDeprecated(bytes32 indexed inventionId, address indexed registrant);

    function register(
        bytes32 packHash,
        bytes32[] calldata parentIds,
        bytes32 licenseHash,
        string calldata contentURI
    ) external returns (bytes32 inventionId) {
        if (packHash == bytes32(0) || licenseHash == bytes32(0)) revert ZeroHash();
        if (parentIds.length > 4) revert TooManyParents();
        for (uint256 i = 0; i < parentIds.length; ++i) {
            if (i > 0 && parentIds[i - 1] >= parentIds[i]) revert ParentsNotStrictlyAscending();
            if (!_exists[parentIds[i]]) revert ParentNotFound(parentIds[i]);
        }
        bytes memory uriBytes = bytes(contentURI);
        if (uriBytes.length > 256) revert URITooLong();
        if (!_isAllowedURI(uriBytes)) revert InvalidContentURI();

        bytes32 authorKey = keccak256(abi.encode(msg.sender, packHash));
        bytes32 existingId = _authorPack[authorKey];
        if (existingId != bytes32(0)) {
            if (_inventions[existingId].licenseHash != licenseHash || !_sameParents(existingId, parentIds) || keccak256(bytes(_contentURIs[existingId])) != keccak256(uriBytes)) {
                revert ConflictingRegistration(existingId);
            }
            return existingId;
        }

        inventionId = keccak256(abi.encode("WF:invention:v1", block.chainid, address(this), msg.sender, packHash));
        _exists[inventionId] = true;
        _inventions[inventionId] = Invention(msg.sender, packHash, licenseHash, uint64(block.number), uint8(parentIds.length), false);
        for (uint256 j = 0; j < parentIds.length; ++j) _parents[inventionId].push(parentIds[j]);
        _contentURIs[inventionId] = contentURI;
        _authorPack[authorKey] = inventionId;
        emit InventionRegistered(inventionId, msg.sender, packHash, parentIds, licenseHash, contentURI);
    }

    function deprecate(bytes32 inventionId) external {
        if (!_exists[inventionId]) revert UnknownInvention(inventionId);
        Invention storage invention = _inventions[inventionId];
        if (invention.registrant != msg.sender) revert NotRegistrant();
        if (invention.deprecated) return;
        invention.deprecated = true;
        emit InventionDeprecated(inventionId, msg.sender);
    }

    function getInvention(bytes32 inventionId) external view returns (Invention memory) {
        if (!_exists[inventionId]) revert UnknownInvention(inventionId);
        return _inventions[inventionId];
    }

    function getParents(bytes32 inventionId) external view returns (bytes32[] memory) {
        if (!_exists[inventionId]) revert UnknownInvention(inventionId);
        return _parents[inventionId];
    }

    function getContentURI(bytes32 inventionId) external view returns (string memory) {
        if (!_exists[inventionId]) revert UnknownInvention(inventionId);
        return _contentURIs[inventionId];
    }

    function _sameParents(bytes32 inventionId, bytes32[] calldata candidate) private view returns (bool) {
        bytes32[] storage stored = _parents[inventionId];
        if (stored.length != candidate.length) return false;
        for (uint256 i = 0; i < stored.length; ++i) if (stored[i] != candidate[i]) return false;
        return true;
    }

    function _isAllowedURI(bytes memory uri) private pure returns (bool) {
        if (uri.length >= 7 && _prefix(uri, "ipfs://")) return true;
        if (uri.length >= 8 && _prefix(uri, "https://")) return true;
        return false;
    }

    function _prefix(bytes memory value, string memory prefix) private pure returns (bool) {
        bytes memory expected = bytes(prefix);
        if (value.length < expected.length) return false;
        for (uint256 i = 0; i < expected.length; ++i) if (value[i] != expected[i]) return false;
        return true;
    }
}
