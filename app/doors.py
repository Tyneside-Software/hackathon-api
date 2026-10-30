"""A door is a first line, a town, and a postcode.

Named buildings keep that building. A home, a sleep door, and any other
point in the circle is a numbered street in the nearest town. The same
coordinate always returns the same door.
"""
from __future__ import annotations

import hashlib
import random

# line, city, postcode. The key is the published place name.
NAMED: dict[str, tuple[str, str, str]] = {
    "Shiremoor": ("18 Park Lane", "Shiremoor", "NE27 0LP"),
    "Backworth": ("4 Station Road", "Backworth", "NE27 0AH"),
    "Earsdon": ("6 Front Street", "Earsdon", "NE25 9JU"),
    "West Monkseaton": ("12 Earsdon Road", "Whitley Bay", "NE25 9SX"),
    "Forest Hall": ("8 Station Road", "Forest Hall", "NE12 9AE"),
    "Killingworth": ("22 Garth Twenty", "Killingworth", "NE12 6DY"),
    "Benton": ("10 Front Street", "Benton", "NE7 7XE"),
    "Cobalt": ("Cobalt Business Park", "Newcastle upon Tyne", "NE27 0QJ"),
    "Silverlink": ("Silverlink Shopping Park", "Wallsend", "NE28 9ND"),
    "Royal Quays": ("Royal Quays", "North Shields", "NE29 6DW"),
    "Regent Centre": ("Regent Centre", "Gosforth", "NE3 3NB"),
    "Four Lane Ends": ("Four Lane Ends Metro", "Longbenton", "NE12 8EU"),
    "Whitley Bay": ("1 Park Avenue", "Whitley Bay", "NE26 1DG"),
    "Spanish City": ("Spanish City", "Whitley Bay", "NE26 1BG"),
    "Cullercoats": ("1 Front Street", "Cullercoats", "NE30 4DZ"),
    "Tynemouth": ("14 Front Street", "Tynemouth", "NE30 4DZ"),
    "North Shields": ("10 Saville Street", "North Shields", "NE30 1NT"),
    "Grey's Monument": ("Grey's Monument", "Newcastle upon Tyne", "NE1 7AL"),
    "Central Station": ("Newcastle Central Station", "Newcastle upon Tyne", "NE1 5DL"),
    "Haymarket": ("Haymarket Metro", "Newcastle upon Tyne", "NE1 7PF"),
    "the Quayside": ("The Quayside", "Newcastle upon Tyne", "NE1 3DX"),
    "Osborne Road": ("86 Osborne Road", "Newcastle upon Tyne", "NE2 2BS"),
    "North Tyneside General at Rake Lane": (
        "North Tyneside General Hospital",
        "North Shields",
        "NE29 8NH",
    ),
    "the RVI": ("Royal Victoria Infirmary", "Newcastle upon Tyne", "NE1 4LP"),
    "the Freeman": ("Freeman Hospital", "Newcastle upon Tyne", "NE7 7DN"),
    "Newcastle Airport": ("Newcastle International Airport", "Newcastle upon Tyne", "NE13 8BZ"),
    "Teesside Airport": ("Teesside International Airport", "Darlington", "DL2 1LU"),
    "Durham Station": ("Durham Station", "Durham", "DH1 1HS"),
    "Darlington Station": ("Darlington Station", "Darlington", "DL1 4AA"),
    "Middlesbrough Station": ("Middlesbrough Station", "Middlesbrough", "TS1 1SU"),
    "Sunderland Station": ("Sunderland Station", "Sunderland", "SR1 3HF"),
    "Hexham Station": ("Hexham Station", "Hexham", "NE46 1EU"),
    "Morpeth Station": ("Morpeth Station", "Morpeth", "NE61 2SE"),
    "Chester-le-Street Station": ("Chester-le-Street Station", "Chester-le-Street", "DH3 3RB"),
    "Hartlepool Station": ("Hartlepool Station", "Hartlepool", "TS24 7ED"),
    "Alnmouth Station": ("Alnmouth Station", "Alnmouth", "NE66 2NP"),
    "QE Gateshead": ("Queen Elizabeth Hospital", "Gateshead", "NE9 6SX"),
    "Sunderland Royal": ("Sunderland Royal Hospital", "Sunderland", "SR4 7TP"),
    "James Cook": ("James Cook University Hospital", "Middlesbrough", "TS4 3BW"),
    "University Hospital North Durham": (
        "University Hospital of North Durham",
        "Durham",
        "DH1 5TW",
    ),
    "NSECH Cramlington": (
        "Northumbria Specialist Emergency Care Hospital",
        "Cramlington",
        "NE23 6NZ",
    ),
    "Wansbeck General": ("Wansbeck General Hospital", "Ashington", "NE63 9JJ"),
    "Darlington Memorial": ("Darlington Memorial Hospital", "Darlington", "DL3 6HX"),
    "University Hospital Hartlepool": (
        "University Hospital of Hartlepool",
        "Hartlepool",
        "TS24 9AH",
    ),
    "Metrocentre": ("Metrocentre", "Gateshead", "NE11 9YG"),
    "Eldon Square": ("Eldon Square", "Newcastle upon Tyne", "NE1 7RA"),
    "Dalton Park": ("Dalton Park", "Murton", "SR7 9HU"),
    "Teesside Park": ("Teesside Park", "Stockton-on-Tees", "TS17 7BN"),
    "The Bridges": ("The Bridges", "Sunderland", "SR1 3DR"),
    "Quorum Business Park": ("Quorum Business Park", "Newcastle upon Tyne", "NE12 8BS"),
    "Newcastle Helix": ("Newcastle Helix", "Newcastle upon Tyne", "NE4 5TG"),
    "Team Valley": ("Team Valley Trading Estate", "Gateshead", "NE11 0QH"),
    "Nissan": ("Nissan", "Sunderland", "SR5 3NS"),
    "NETPark": ("NETPark", "Sedgefield", "TS21 3FD"),
    "Aykley Heads": ("Aykley Heads", "Durham", "DH1 5TS"),
    "Wilton": ("Wilton International", "Redcar", "TS10 4RF"),
    "Port of Tyne": ("Port of Tyne", "South Shields", "NE34 9PT"),
    "Newcastle University": ("Newcastle University", "Newcastle upon Tyne", "NE1 7RU"),
    "Northumbria University": ("Northumbria University", "Newcastle upon Tyne", "NE1 8ST"),
    "University of Sunderland": ("University of Sunderland", "Sunderland", "SR1 3SD"),
    "Teesside University": ("Teesside University", "Middlesbrough", "TS1 3BX"),
    "Durham Cathedral": ("Durham Cathedral", "Durham", "DH1 3EH"),
    "St James' Park": ("St James' Park", "Newcastle upon Tyne", "NE1 4ST"),
    "Stadium of Light": ("Stadium of Light", "Sunderland", "SR5 1SU"),
    "Riverside Stadium": ("Riverside Stadium", "Middlesbrough", "TS3 6RS"),
    "Kingston Park": ("Kingston Park Stadium", "Newcastle upon Tyne", "NE13 8NY"),
    "North Shields Ferry": ("North Shields Ferry", "North Shields", "NE30 1JH"),
    "Vermont Hotel": ("Vermont Hotel", "Newcastle upon Tyne", "NE1 1RQ"),
    "Hilton Newcastle Gateshead": ("Hilton Newcastle Gateshead", "Gateshead", "NE8 2AR"),
    "Sandman Signature": ("Sandman Signature", "Newcastle upon Tyne", "NE1 5DH"),
    "Maldron Hotel": ("Maldron Hotel", "Newcastle upon Tyne", "NE1 5TG"),
    "Grand Hotel Tynemouth": ("Grand Hotel", "Tynemouth", "NE30 4ER"),
    "Ramside Hall": ("Ramside Hall", "Durham", "DH1 1TD"),
    "Rockliffe Hall": ("Rockliffe Hall", "Hurworth", "DL2 2DU"),
    "Bigg Market": ("Bigg Market", "Newcastle upon Tyne", "NE1 1UW"),
    "Walkergate": ("18 Walkergate", "Durham", "DH1 1QQ"),
    "Linthorpe Road": ("88 Linthorpe Road", "Middlesbrough", "TS1 5BS"),
    "Seaburn": ("Seaburn Promenade", "Sunderland", "SR6 8AA"),
    "Redcar Esplanade": ("The Esplanade", "Redcar", "TS10 3AE"),
    "Seaham Marina": ("Seaham Marina", "Seaham", "SR7 7EE"),
    "Bamburgh Castle": ("Bamburgh Castle", "Bamburgh", "NE69 7DF"),
    "Beamish Museum": ("Beamish Museum", "Beamish", "DH9 1DP"),
    "Angel of the North": ("Angel of the North", "Gateshead", "NE9 7TY"),
    "Alnwick Castle": ("Alnwick Castle", "Alnwick", "NE66 1NQ"),
}

# The yard. It is not the Shiremoor town pin.
DEPOT_DOOR = ("42 Earsdon Road", "Shiremoor", "NE27 0HJ")

# Town, then streets that belong to that town: street, postcode.
# The city line is the town a driver would write. Newcastle and Stockton use the full name.
STREETS: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "Newcastle": (
        "Newcastle upon Tyne",
        (
            ("Grey Street", "NE1 6EE"),
            ("Clayton Street", "NE1 5PY"),
            ("Stowell Street", "NE1 4XQ"),
            ("Pudding Chare", "NE1 1UE"),
        ),
    ),
    "Gateshead": (
        "Gateshead",
        (
            ("High Street", "NE8 1EN"),
            ("Jackson Street", "NE8 1EE"),
            ("Charles Street", "NE8 1HE"),
            ("Askew Road", "NE8 2AU"),
        ),
    ),
    "Sunderland": (
        "Sunderland",
        (
            ("High Street West", "SR1 3HA"),
            ("Fawcett Street", "SR1 1RH"),
            ("Bridge Street", "SR1 1TE"),
            ("Toward Road", "SR2 7UD"),
        ),
    ),
    "South Shields": (
        "South Shields",
        (
            ("King Street", "NE33 1HT"),
            ("Ocean Road", "NE33 2HT"),
            ("Fowler Street", "NE33 1NS"),
            ("Westoe Road", "NE33 3PA"),
        ),
    ),
    "Jarrow": (
        "Jarrow",
        (
            ("Grange Road", "NE32 3LH"),
            ("Ellison Street", "NE32 3JY"),
            ("Albert Road", "NE32 5DT"),
            ("Western Road", "NE32 3HR"),
        ),
    ),
    "North Shields": (
        "North Shields",
        (
            ("Saville Street", "NE30 1NT"),
            ("Bedford Street", "NE29 0AL"),
            ("Nile Street", "NE29 0BE"),
            ("Church Way", "NE29 0AE"),
        ),
    ),
    "Tynemouth": (
        "Tynemouth",
        (
            ("Front Street", "NE30 4DZ"),
            ("Hotspur Street", "NE30 4EN"),
            ("Percy Park", "NE30 4HA"),
            ("Manor Road", "NE30 4RJ"),
        ),
    ),
    "Whitley Bay": (
        "Whitley Bay",
        (
            ("Park Avenue", "NE26 1DG"),
            ("Victoria Terrace", "NE26 2QW"),
            ("Esplanade", "NE26 2HJ"),
            ("Marine Avenue", "NE26 1LZ"),
        ),
    ),
    "Wallsend": (
        "Wallsend",
        (
            ("High Street East", "NE28 7AT"),
            ("Station Road", "NE28 8EN"),
            ("Carville Road", "NE28 6HJ"),
            ("Park Road", "NE28 7LS"),
        ),
    ),
    "Gosforth": (
        "Gosforth",
        (
            ("High Street", "NE3 1HB"),
            ("Salters Road", "NE3 1DJ"),
            ("Church Road", "NE3 1UJ"),
            ("Hollywood Avenue", "NE3 5BH"),
        ),
    ),
    "Heaton": (
        "Heaton",
        (
            ("Chillingham Road", "NE6 5HN"),
            ("Cardigan Terrace", "NE6 5PU"),
            ("Heaton Road", "NE6 1SA"),
            ("Tenth Avenue", "NE6 5JS"),
        ),
    ),
    "Shiremoor": (
        "Shiremoor",
        (
            ("Earsdon Road", "NE27 0HJ"),
            ("Park Lane", "NE27 0LP"),
            ("Brenkley Avenue", "NE27 0ND"),
            ("Bertram Place", "NE27 0PR"),
        ),
    ),
    "Killingworth": (
        "Killingworth",
        (
            ("Great Lime Road", "NE12 6RU"),
            ("West Bailey", "NE12 6BH"),
            ("Garth Twenty", "NE12 6DY"),
            ("Citadel East", "NE12 6SS"),
        ),
    ),
    "Cramlington": (
        "Cramlington",
        (
            ("Village Road", "NE23 1DN"),
            ("Dudley Lane", "NE23 6QW"),
            ("Nelson Drive", "NE23 1HJ"),
            ("Forum Way", "NE23 6YB"),
        ),
    ),
    "Blyth": (
        "Blyth",
        (
            ("Regent Street", "NE24 1LQ"),
            ("Bridge Street", "NE24 2AP"),
            ("Waterloo Road", "NE24 1DF"),
            ("Beaconsfield Street", "NE24 2DS"),
        ),
    ),
    "Ashington": (
        "Ashington",
        (
            ("Station Road", "NE63 9HJ"),
            ("Woodhorn Road", "NE63 0AE"),
            ("Lintonville Terrace", "NE63 9UN"),
            ("Laburnum Terrace", "NE63 8AF"),
        ),
    ),
    "Bedlington": (
        "Bedlington",
        (
            ("Front Street West", "NE22 5TT"),
            ("Market Place", "NE22 5TN"),
            ("Stead Lane", "NE22 5JS"),
            ("Vicarage Drive", "NE22 5RH"),
        ),
    ),
    "Morpeth": (
        "Morpeth",
        (
            ("Bridge Street", "NE61 1NB"),
            ("Newgate Street", "NE61 1BA"),
            ("Castle Square", "NE61 1YL"),
            ("Manchester Street", "NE61 1BH"),
        ),
    ),
    "Ponteland": (
        "Ponteland",
        (
            ("Main Street", "NE20 9NH"),
            ("Merton Way", "NE20 9PX"),
            ("Broadway", "NE20 9PW"),
            ("Thornhill Road", "NE20 9QB"),
        ),
    ),
    "Hexham": (
        "Hexham",
        (
            ("Fore Street", "NE46 1ND"),
            ("Battle Hill", "NE46 1BA"),
            ("Priestpopple", "NE46 1PH"),
            ("Hencotes", "NE46 2EQ"),
        ),
    ),
    "Prudhoe": (
        "Prudhoe",
        (
            ("Front Street", "NE42 5PU"),
            ("Station Road", "NE42 5HA"),
            ("South Road", "NE42 5JS"),
            ("Castle Road", "NE42 6ND"),
        ),
    ),
    "Washington": (
        "Washington",
        (
            ("Village Lane", "NE38 7AB"),
            ("Blue House Lane", "NE38 7HJ"),
            ("The Galleries", "NE38 7SA"),
            ("Concord", "NE37 2SY"),
        ),
    ),
    "Chester-le-Street": (
        "Chester-le-Street",
        (
            ("Front Street", "DH3 3BD"),
            ("Newcastle Road", "DH3 3TS"),
            ("Ropery Lane", "DH3 3AJ"),
            ("Ash Grove", "DH3 3RJ"),
        ),
    ),
    "Durham": (
        "Durham",
        (
            ("Saddler Street", "DH1 3NP"),
            ("North Road", "DH1 4SG"),
            ("Crossgate", "DH1 4PS"),
            ("Gilesgate", "DH1 1QW"),
        ),
    ),
    "Stanley": (
        "Stanley",
        (
            ("Front Street", "DH9 0JE"),
            ("Clifford Road", "DH9 0AB"),
            ("High Street", "DH9 0TA"),
            ("Scott Street", "DH9 8AD"),
        ),
    ),
    "Consett": (
        "Consett",
        (
            ("Front Street", "DH8 5AB"),
            ("Middle Street", "DH8 5QP"),
            ("Medomsley Road", "DH8 5HL"),
            ("Parliament Street", "DH8 5DH"),
        ),
    ),
    "Peterlee": (
        "Peterlee",
        (
            ("Yoden Way", "SR8 1AL"),
            ("Burnhope Way", "SR8 1ET"),
            ("The Eden", "SR8 5AW"),
            ("Bede Way", "SR8 1AD"),
        ),
    ),
    "Seaham": (
        "Seaham",
        (
            ("Church Street", "SR7 7HF"),
            ("North Terrace", "SR7 7EU"),
            ("Adolphus Place", "SR7 7HY"),
            ("Tempest Road", "SR7 7DR"),
        ),
    ),
    "Hartlepool": (
        "Hartlepool",
        (
            ("Church Street", "TS24 7DJ"),
            ("York Road", "TS26 8AB"),
            ("Victoria Road", "TS26 8DF"),
            ("Avenue Road", "TS24 8AT"),
        ),
    ),
    "Alnwick": (
        "Alnwick",
        (
            ("Bondgate Within", "NE66 1TD"),
            ("Fenkle Street", "NE66 1HW"),
            ("Market Street", "NE66 1SS"),
            ("Narrowgate", "NE66 1JG"),
        ),
    ),
    "Amble": (
        "Amble",
        (
            ("Queen Street", "NE65 0DQ"),
            ("High Street", "NE65 0LD"),
            ("Church Street", "NE65 0DZ"),
            ("Leazes Street", "NE65 0BJ"),
        ),
    ),
    "Rothbury": (
        "Rothbury",
        (
            ("High Street", "NE65 7TE"),
            ("Bridge Street", "NE65 7SD"),
            ("Townfoot", "NE65 7SL"),
            ("Front Street", "NE65 7TZ"),
        ),
    ),
    "Wooler": (
        "Wooler",
        (
            ("High Street", "NE71 6LD"),
            ("Market Place", "NE71 6LH"),
            ("Cheviot Street", "NE71 6BA"),
            ("Church Street", "NE71 6DA"),
        ),
    ),
    "Seahouses": (
        "Seahouses",
        (
            ("Main Street", "NE68 7RF"),
            ("Harbour Road", "NE68 7RN"),
            ("King Street", "NE68 7XP"),
            ("North Street", "NE68 7UE"),
        ),
    ),
    "Belford": (
        "Belford",
        (
            ("High Street", "NE70 7NG"),
            ("West Street", "NE70 7QE"),
            ("Church Street", "NE70 7LS"),
            ("Nursery Lane", "NE70 7DT"),
        ),
    ),
    "Bellingham": (
        "Bellingham",
        (
            ("Front Street", "NE48 2AA"),
            ("Fountain Terrace", "NE48 2BJ"),
            ("Rede Square", "NE48 2EH"),
            ("West View", "NE48 2DL"),
        ),
    ),
    "Haltwhistle": (
        "Haltwhistle",
        (
            ("Main Street", "NE49 0AX"),
            ("Westgate", "NE49 9AF"),
            ("Aesica Road", "NE49 0HJ"),
            ("Lanty's Lonnen", "NE49 9BP"),
        ),
    ),
    "Allendale": (
        "Allendale",
        (
            ("Market Place", "NE47 9BD"),
            ("Shield Street", "NE47 9BP"),
            ("Leadside", "NE47 9EH"),
            ("Deneside", "NE47 9HJ"),
        ),
    ),
    "Corbridge": (
        "Corbridge",
        (
            ("Front Street", "NE45 5AT"),
            ("Princes Street", "NE45 5DG"),
            ("Hill Street", "NE45 5AA"),
            ("Watling Street", "NE45 5AG"),
        ),
    ),
    "Alston": (
        "Alston",
        (
            ("Front Street", "CA9 3HU"),
            ("Market Place", "CA9 3QN"),
            ("King's Arms Lane", "CA9 3JF"),
            ("Church Road", "CA9 3QU"),
        ),
    ),
    "Brampton": (
        "Brampton",
        (
            ("Front Street", "CA8 1NG"),
            ("Main Street", "CA8 1RS"),
            ("Moat Street", "CA8 1ED"),
            ("High Cross Street", "CA8 1AR"),
        ),
    ),
    "Stanhope": (
        "Stanhope",
        (
            ("Front Street", "DL13 2UE"),
            ("Market Place", "DL13 2UN"),
            ("Hood Street", "DL13 2YH"),
            ("Butts Crescent", "DL13 2GB"),
        ),
    ),
    "Bishop Auckland": (
        "Bishop Auckland",
        (
            ("Newgate Street", "DL14 7EG"),
            ("Fore Bondgate", "DL14 7PE"),
            ("Market Place", "DL14 7NP"),
            ("Durham Road", "DL14 6HX"),
        ),
    ),
    "Crook": (
        "Crook",
        (
            ("Hope Street", "DL15 9HS"),
            ("North Terrace", "DL15 9ES"),
            ("Commercial Street", "DL15 9RP"),
            ("Wheatbottom", "DL15 9HA"),
        ),
    ),
    "Spennymoor": (
        "Spennymoor",
        (
            ("High Street", "DL16 6DB"),
            ("Cheapside", "DL16 6DJ"),
            ("King Street", "DL16 6AS"),
            ("Durham Road", "DL16 6SJ"),
        ),
    ),
    "Ferryhill": (
        "Ferryhill",
        (
            ("Market Street", "DL17 8JN"),
            ("North Street", "DL17 8HX"),
            ("Durham Road", "DL17 8LL"),
            ("Linden Road", "DL17 8PW"),
        ),
    ),
    "Sedgefield": (
        "Sedgefield",
        (
            ("Front Street", "TS21 3AT"),
            ("West End", "TS21 2BW"),
            ("Rectory Row", "TS21 2AJ"),
            ("Durham Road", "TS21 3DW"),
        ),
    ),
    "Newton Aycliffe": (
        "Newton Aycliffe",
        (
            ("Central Avenue", "DL5 5NP"),
            ("Stephenson Way", "DL5 6DX"),
            ("Pease Way", "DL5 5YH"),
            ("Burn Lane", "DL5 4ET"),
        ),
    ),
    "Darlington": (
        "Darlington",
        (
            ("High Row", "DL1 5PW"),
            ("Skinnergate", "DL3 7NB"),
            ("Bondgate", "DL3 7JR"),
            ("Duke Street", "DL3 7RX"),
        ),
    ),
    "Barnard Castle": (
        "Barnard Castle",
        (
            ("Market Place", "DL12 8ND"),
            ("Galgate", "DL12 8BH"),
            ("Horsemarket", "DL12 8LZ"),
            ("Newgate", "DL12 8NG"),
        ),
    ),
    "Stockton": (
        "Stockton-on-Tees",
        (
            ("High Street", "TS18 1BH"),
            ("Dovecot Street", "TS18 1LL"),
            ("Ramsgate", "TS18 1BS"),
            ("Yarm Lane", "TS18 3DT"),
        ),
    ),
    "Billingham": (
        "Billingham",
        (
            ("Queensway", "TS23 2LT"),
            ("Town Square", "TS23 2NA"),
            ("Station Road", "TS23 1AE"),
            ("Marsh House Avenue", "TS23 3ER"),
        ),
    ),
    "Middlesbrough": (
        "Middlesbrough",
        (
            ("Borough Road", "TS1 4AD"),
            ("Albert Road", "TS1 1QA"),
            ("Grange Road", "TS1 5BA"),
            ("Linthorpe Road", "TS1 5BS"),
        ),
    ),
    "Yarm": (
        "Yarm",
        (
            ("High Street", "TS15 9BG"),
            ("Bridge Street", "TS15 9BU"),
            ("Bentley Wynd", "TS15 9BS"),
            ("West Street", "TS15 9BU"),
        ),
    ),
    "Redcar": (
        "Redcar",
        (
            ("High Street", "TS10 3BZ"),
            ("Queen Street", "TS10 1DY"),
            ("Station Road", "TS10 1AN"),
            ("Coatham Road", "TS10 1RH"),
        ),
    ),
    "Guisborough": (
        "Guisborough",
        (
            ("Westgate", "TS14 6BH"),
            ("Church Street", "TS14 6HG"),
            ("Bow Street", "TS14 6BP"),
            ("Walkers Row", "TS14 6EL"),
        ),
    ),
    "Northallerton": (
        "Northallerton",
        (
            ("High Street", "DL7 8PP"),
            ("Zetland Street", "DL6 1NA"),
            ("Boroughbridge Road", "DL7 8AN"),
            ("Romanby Road", "DL7 8FH"),
        ),
    ),
    "Richmond": (
        "Richmond",
        (
            ("Market Place", "DL10 4QL"),
            ("Frenchgate", "DL10 4JG"),
            ("Newbiggin", "DL10 4DX"),
            ("King Street", "DL10 4HP"),
        ),
    ),
}


def _pack(parts: tuple[str, str, str]) -> dict[str, str]:
    return {"line": parts[0], "city": parts[1], "postcode": parts[2]}


def named_door(name: str) -> dict[str, str] | None:
    """The published door for this building or town pin. None when it is not one."""
    found = NAMED.get(name)
    if found is None:
        return None
    return _pack(found)


def depot_door() -> dict[str, str]:
    """The Shiremoor yard. Every car parked on that pin shares it."""
    return _pack(DEPOT_DOOR)


def _seed(lat: float, lng: float) -> int:
    raw = f"{round(float(lat), 5):.5f}|{round(float(lng), 5):.5f}".encode()
    return int(hashlib.sha256(raw).hexdigest()[:12], 16)


def street_address(settlement: str, lat: float, lng: float) -> dict[str, str]:
    """A numbered street in this town. The coordinate picks the door, and keeps it."""
    city, streets = STREETS.get(settlement) or STREETS["Newcastle"]
    rng = random.Random(_seed(lat, lng))
    number = 1 + rng.randrange(160)
    street, postcode = streets[rng.randrange(len(streets))]
    return {"line": f"{number} {street}", "city": city, "postcode": postcode}
