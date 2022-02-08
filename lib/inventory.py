#!/usr/bin/env python
# -*- coding: utf-8 -*-

###########################################################################
# Copyright (C) GFZ Potsdam                                               #
# All rights reserved.                                                    #
#                                                                         #
# Author: Joachim Saul (saul@gfz-potsdam.de)                              #
#                                                                         #
# GNU Affero General Public License Usage                                 #
# This file may be used under the terms of the GNU Affero                 #
# Public License version 3.0 as published by the Free Software Foundation #
# and appearing in the file LICENSE included in the packaging of this     #
# file. Please review the following information to ensure the GNU Affero  #
# Public License version 3.0 requirements will be met:                    #
# https://www.gnu.org/licenses/agpl-3.0.html.                             #
###########################################################################


def operational(obj, time):
    """
    Return True if the inventory item 'obj' is considered
    operational at the specified time. False is returned otherwise.
    """
    # If the start time of an inventory item is not
    # known, it is not considered operational.
    try:
        start = obj.start()
        assert time >= start
    except:
        return False

    # If the end time of an inventory item is not
    # known it is considered "open end".
    try:
        end = obj.end()
        if time > end:
            return False
    except:
        pass

    return True


def InventoryIterator(inventory, time=None):
    """
    inventory is a SeisComP inventory instance. Note that this needs
    to be an inventory incl. the streams. Otherwise this iterator
    makes no sense.
    """

    for inet in range(inventory.networkCount()):
        network = inventory.network(inet)
        if time is not None and not operational(network, time):
            continue

        for ista in range(network.stationCount()):
            station = network.station(ista)

            if time is not None and not operational(station, time):
                continue

            for iloc in range(station.sensorLocationCount()):
                location = station.sensorLocation(iloc)

                if time is not None and not operational(location, time):
                    continue
                for istr in range(location.streamCount()):
                    stream = location.stream(istr)

                    if time is not None and not operational(stream, time):
                        continue

                    yield network, station, location, stream
