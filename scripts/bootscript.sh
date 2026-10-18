#!/bin/bash
# Configures a WireGuard or OpenVPN server and uploads client profiles.
# Runs as root from cloud-init; configs and the env file are written to /opt/vpn by cloud-init.

set -euo pipefail

VPN_DIR=/opt/vpn
OUT_DIR="${VPN_DIR}/out"
# shellcheck source=/dev/null
source "${VPN_DIR}/env"
: "${SERVER_IP:?}" "${PROTOCOL:?}" "${VPN_PORT:?}" "${VPN_TRANSPORT:?}"

export DEBIAN_FRONTEND=noninteractive

setup_openvpn() {
    apt-get install -y openvpn easy-rsa

    export EASYRSA_BATCH=1
    export EASYRSA_PKI="${VPN_DIR}/pki"
    export EASYRSA_ALGO=ec
    export EASYRSA_DIGEST=sha512
    local easyrsa=/usr/share/easy-rsa/easyrsa

    "${easyrsa}" init-pki
    "${easyrsa}" build-ca nopass
    "${easyrsa}" build-server-full server nopass
    "${easyrsa}" build-client-full client nopass
    openvpn --genkey secret "${EASYRSA_PKI}/ta.key"

    install -m 600 -t /etc/openvpn/server/ \
        "${EASYRSA_PKI}/ca.crt" \
        "${EASYRSA_PKI}/issued/server.crt" \
        "${EASYRSA_PKI}/private/server.key" \
        "${EASYRSA_PKI}/ta.key"
    install -m 644 "${VPN_DIR}/openvpn/server.conf" /etc/openvpn/server/server.conf

    {
        sed "s/__SERVER_IP__/${SERVER_IP}/" "${VPN_DIR}/openvpn/client.conf"
        echo "<ca>";        cat "${EASYRSA_PKI}/ca.crt";                         echo "</ca>"
        echo "<cert>";      openssl x509 -in "${EASYRSA_PKI}/issued/client.crt"; echo "</cert>"
        echo "<key>";       cat "${EASYRSA_PKI}/private/client.key";             echo "</key>"
        echo "<tls-crypt>"; cat "${EASYRSA_PKI}/ta.key";                         echo "</tls-crypt>"
    } > "${OUT_DIR}/client.ovpn"

    SERVICE=openvpn-server@server.service
}

setup_wireguard() {
    : "${WIREGUARD_PEERS:?}"
    apt-get install -y wireguard-tools qrencode

    local server_key server_pub cfg=/etc/wireguard/wg0.conf
    server_key=$(wg genkey)
    server_pub=$(echo "${server_key}" | wg pubkey)
    sed -e "s|__PORT__|${VPN_PORT}|" \
        -e "s|__SERVER_PRIVATE_KEY__|${server_key}|" \
        "${VPN_DIR}/wireguard/wg0.conf" > "${cfg}"
    chmod 600 "${cfg}"

    local peer key psk address host=2
    for peer in ${WIREGUARD_PEERS}; do
        key=$(wg genkey)
        psk=$(wg genpsk)
        address="10.8.0.${host}"
        host=$((host + 1))

        printf '\n[Peer]\n# %s\nPublicKey = %s\nPresharedKey = %s\nAllowedIPs = %s/32\n' \
            "${peer}" "$(echo "${key}" | wg pubkey)" "${psk}" "${address}" >> "${cfg}"

        sed -e "s|__CLIENT_PRIVATE_KEY__|${key}|" \
            -e "s|__CLIENT_ADDRESS__|${address}|" \
            -e "s|__SERVER_PUBLIC_KEY__|${server_pub}|" \
            -e "s|__PRESHARED_KEY__|${psk}|" \
            -e "s|__SERVER_IP__|${SERVER_IP}|" \
            -e "s|__PORT__|${VPN_PORT}|" \
            "${VPN_DIR}/wireguard/client.conf" > "${OUT_DIR}/${peer}.conf"
        qrencode -t png -s 8 -m 2 -o "${OUT_DIR}/${peer}.png" -r "${OUT_DIR}/${peer}.conf"
    done

    SERVICE=wg-quick@wg0.service
}

setup_firewall() {
    echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-vpn.conf
    sysctl -p /etc/sysctl.d/99-vpn.conf

    local iface
    iface=$(ip -4 route show default | awk '{ print $5; exit }')
    if ! grep -q "START VPN RULES" /etc/ufw/before.rules; then
        sed "s/__IFACE__/${iface}/" "${VPN_DIR}/nat.rules" | cat - /etc/ufw/before.rules > /etc/ufw/before.rules.new
        mv /etc/ufw/before.rules.new /etc/ufw/before.rules
    fi
    sed -i 's/^DEFAULT_FORWARD_POLICY=.*/DEFAULT_FORWARD_POLICY="ACCEPT"/' /etc/default/ufw

    ufw allow "${VPN_PORT}/${VPN_TRANSPORT}"
    ufw allow OpenSSH
    ufw --force enable
    ufw reload
}

upload_profiles() {
    if [[ ! -f "${VPN_DIR}/upload.curlrc" ]]; then
        echo "No upload credentials, profiles are left in ${OUT_DIR}"
        return
    fi
    local file
    for file in "${OUT_DIR}"/*; do
        curl -fsS --retry 5 --retry-all-errors -K "${VPN_DIR}/upload.curlrc" -F "file=@${file}" > /dev/null
        echo "Uploaded $(basename "${file}")"
    done

    # Uploaded last: the bucket notifies the bot about this marker, see terraform/bot
    date -u +%Y-%m-%dT%H:%M:%SZ > "${VPN_DIR}/ready"
    curl -fsS --retry 5 --retry-all-errors -K "${VPN_DIR}/upload.curlrc" -F "file=@${VPN_DIR}/ready" > /dev/null
    echo "Uploaded the ready marker"
}

apt-get update
apt-get install -y ufw curl

install -d -m 700 "${OUT_DIR}"

case "${PROTOCOL}" in
    openvpn)   setup_openvpn ;;
    wireguard) setup_wireguard ;;
    *)         echo "Unknown protocol: ${PROTOCOL}" >&2; exit 1 ;;
esac

chmod 600 "${OUT_DIR}"/*
setup_firewall
systemctl enable --now "${SERVICE}"
upload_profiles
