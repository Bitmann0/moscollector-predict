#!/bin/sh
# ТОЛЬКО ДЛЯ ТЕСТА: точка входа сервиса ldap в compose.ldap.yaml.
#
# Образ osixia/openldap:1.5.0 подписывает сертификат сервера своим CA, а срок этого CA
# кончился 15.01.2026: такой сертификат не проходит проверку. Поэтому скрипт выпускает
# свой CA и сертификат сервера на имена ldap, localhost и 127.0.0.1, если в томе
# сертификатов их нет или цепочка перестанет проверяться в ближайшие сутки. Ключ CA
# удаляется сразу после подписи. Сертификат, который лежит в томе, образ не перевыпускает.
set -e
dir=/container/service/slapd/assets/certs
tomorrow=$(( $(date +%s) + 86400 ))
if ! openssl verify -CAfile "$dir/ca.crt" -attime "$tomorrow" "$dir/ldap.crt" >/dev/null 2>&1; then
  tmp=$(mktemp -d)
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=Moscollector test LDAP CA" \
    -keyout "$tmp/ca.key" -out "$dir/ca.crt" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj "/CN=ldap" \
    -keyout "$dir/ldap.key" -out "$tmp/ldap.csr" 2>/dev/null
  printf 'subjectAltName=DNS:ldap,DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n' \
    > "$tmp/ext"
  openssl x509 -req -in "$tmp/ldap.csr" -CA "$dir/ca.crt" -CAkey "$tmp/ca.key" \
    -CAserial "$tmp/ca.srl" -CAcreateserial -days 365 -extfile "$tmp/ext" \
    -out "$dir/ldap.crt" 2>/dev/null
  rm -rf "$tmp"
  chown openldap:openldap "$dir/ca.crt" "$dir/ldap.crt" "$dir/ldap.key"
  chmod 644 "$dir/ca.crt" "$dir/ldap.crt"
  chmod 600 "$dir/ldap.key"
  echo "tls-init: выпущены тестовый CA и сертификат сервера ldap"
fi
exec /container/tool/run "$@"
