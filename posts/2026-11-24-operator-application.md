# Operator application — FLOP Testnet validator candidate (draft for the GitHub form)

Form: https://github.com/flop-labs/network/issues/new?template=operator-application.yml
Title the form generates: `operator: technocore-pulse`

## Team ID
technocore-pulse

## Network
testnet

## Requested role
validator candidate

## Existing application or introduction
None for the validator role. Same operator as the public technocore-pulse toolkit (github.com/VolksTiger/technocore-pulse) and the agent wallet in access request network#176.

## Planned capacity and host
Requested validator seats / existing FLOP seats: 1 requested / 0 existing
RPC nodes offered (active / standby): 0 / 1 standby keyless RPC replica after the rehearsal, if the pool wants it
Provider / region: Hetzner dedicated bare metal, Falkenstein or Helsinki (EU); will move region if geographic spread is short elsewhere
CPU / RAM / SSD capacity / OS: AMD Ryzen 9 7950X3D (16 physical cores, 4.2 GHz base) / 128 GB DDR5 ECC / 2 x 1.92 TB NVMe (3.84 TB, RAID 0 or two volumes as the kit prefers) / Ubuntu 24.04 LTS amd64, 1 Gbps
Public host label or P2P address (if prepared): not prepared yet; one dedicated Linux host, Node.js 24+, Docker Engine 28+, Compose, flock; public TCP 30333 and filtered 7001, RPC 9944 / metrics 9615 / raw DA 7000 private

## Preparation stage
planning a host

## Sync and restart evidence
Will follow: keyless Devnet RPC replica in observer mode per the prep kit, two receipts (genesis + finalized hash at a common height, restart on the same volume with the same peer ID at a higher height), posted here as public values only.

## Optional RPC pool contribution
One standby keyless replica can stay up after qualification; mTLS only after FLOP supplies the client CA and edge egress addresses.

## Questions or blockers
Requesting reviewed sponsorship for the validator bond. Please open a private coordination channel for on-call contacts and the per-host worksheet. Host is being rented now; receipts follow within days.

## Before submitting
- [x] I checked for an existing application for this team and network and will keep evidence in one thread.
- [x] This application contains only public information and no secrets or private configuration files.
- [x] I understand that applying, syncing, and RPC pool participation do not grant validator activation, funding, rewards, or a Testnet seat.
