#!/bin/bash
# Replacement start-modem.sh for Huawei E3372h
# Installed by VenusOS-E3372h package

. /opt/victronenergy/serial-starter/run-service.sh

app=/data/VenusOS-E3372h/dbus-modem-e3372.py

start -s /dev/$tty
