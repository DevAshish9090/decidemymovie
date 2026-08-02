"""
Curated collections — precise, hand-verified movie lists for every category that
can't be answered from TMDB's structured data (genres are handled by /api/browse;
these are the subjective/curated moods).

    GET /api/collection?id=<mood_id>  ->  { "picks": [ ... ] }

Each list is (title, year). We resolve every entry to a real TMDB movie via
title+year search (no brittle hardcoded IDs), attach watch-providers, de-dupe,
preserve order, and cache for an hour. The `id` matches the mood card's id in the
frontend, so the card just asks for its own collection.
"""

import asyncio

from fastapi import APIRouter, HTTPException, Query

from ..cache import cache
from ..schemas import Pick
from ..tmdb import tmdb

router = APIRouter(prefix="/api", tags=["collections"])

LIMIT = 30

# id -> (why-label, [ (title, year), ... ])
COLLECTIONS: dict[str, tuple[str, list[tuple[str, int]]]] = {

    "oscar_winners": ("Academy Award winner — Best Picture.", [
        ("Oppenheimer", 2023), ("Everything Everywhere All at Once", 2022), ("CODA", 2021),
        ("Nomadland", 2020), ("Parasite", 2019), ("Green Book", 2018), ("The Shape of Water", 2017),
        ("Moonlight", 2016), ("Spotlight", 2015), ("Birdman", 2014), ("12 Years a Slave", 2013),
        ("Argo", 2012), ("The Artist", 2011), ("The King's Speech", 2010), ("The Hurt Locker", 2008),
        ("Slumdog Millionaire", 2008), ("No Country for Old Men", 2007), ("The Departed", 2006),
        ("Million Dollar Baby", 2004), ("The Lord of the Rings: The Return of the King", 2003),
        ("Chicago", 2002), ("A Beautiful Mind", 2001), ("Gladiator", 2000), ("American Beauty", 1999),
        ("Titanic", 1997), ("Braveheart", 1995), ("Forrest Gump", 1994), ("Schindler's List", 1993),
        ("The Silence of the Lambs", 1991), ("The Godfather", 1972),
    ]),

    "dark_psych": ("A dark psychological thriller.", [
        ("Se7en", 1995), ("Fight Club", 1999), ("Black Swan", 2010), ("Shutter Island", 2010),
        ("Prisoners", 2013), ("Gone Girl", 2014), ("Zodiac", 2007), ("Nightcrawler", 2014),
        ("Oldboy", 2003), ("Memento", 2000), ("Requiem for a Dream", 2000), ("Joker", 2019),
        ("Perfect Blue", 1997), ("The Machinist", 2004), ("Enemy", 2013), ("Mulholland Drive", 2001),
        ("American Psycho", 2000), ("The Sixth Sense", 1999), ("Get Out", 2017), ("Taxi Driver", 1976),
        ("The Silence of the Lambs", 1991), ("Whiplash", 2014),
    ]),

    "interstellar": ("Epic, mind-bending science fiction.", [
        ("Interstellar", 2014), ("Inception", 2010), ("Arrival", 2016), ("Blade Runner 2049", 2017),
        ("The Martian", 2015), ("Gravity", 2013), ("Dune", 2021), ("2001: A Space Odyssey", 1968),
        ("Contact", 1997), ("Ad Astra", 2019), ("Moon", 2009), ("Sunshine", 2007), ("Tenet", 2020),
        ("Annihilation", 2018), ("District 9", 2009), ("Edge of Tomorrow", 2014), ("Ex Machina", 2014),
        ("Predestination", 2014), ("Prometheus", 2012), ("Gattaca", 1997), ("Solaris", 1972),
    ]),

    "slow_cinema": ("Slow, contemplative cinema.", [
        ("Nomadland", 2020), ("The Tree of Life", 2011), ("Paris, Texas", 1984), ("Stalker", 1979),
        ("Drive My Car", 2021), ("Columbus", 2017), ("A Ghost Story", 2017), ("First Cow", 2019),
        ("Certified Copy", 2010), ("The Turin Horse", 2011), ("Roma", 2018), ("Perfect Days", 2023),
        ("Wings of Desire", 1987), ("Days of Heaven", 1978), ("Lost in Translation", 2003),
        ("The Assassin", 2015), ("Winter Light", 1963), ("Uncle Boonmee Who Can Recall His Past Lives", 2010),
    ]),

    "cinematography": ("Breathtaking cinematography.", [
        ("Blade Runner 2049", 2017), ("The Revenant", 2015), ("Roma", 2018), ("In the Mood for Love", 2000),
        ("1917", 2019), ("The Tree of Life", 2011), ("Barry Lyndon", 1975), ("Days of Heaven", 1978),
        ("The Grand Budapest Hotel", 2014), ("Lawrence of Arabia", 1962), ("Hero", 2002),
        ("Come and See", 1985), ("The Fall", 2006), ("Life of Pi", 2012), ("Mad Max: Fury Road", 2015),
        ("Moonlight", 2016), ("The New World", 2005), ("Nomadland", 2020), ("Baraka", 1992),
    ]),

    "minimal_dialogue": ("Told with almost no words.", [
        ("A Quiet Place", 2018), ("All Is Lost", 2013), ("WALL-E", 2008), ("Drive", 2011),
        ("No Country for Old Men", 2007), ("The Revenant", 2015), ("2001: A Space Odyssey", 1968),
        ("Mad Max: Fury Road", 2015), ("The Artist", 2011), ("Gravity", 2013), ("Cast Away", 2000),
        ("Le Samouraï", 1967), ("Valhalla Rising", 2009), ("The Red Turtle", 2016),
        ("Quest for Fire", 1981), ("Buried", 2010), ("Only God Forgives", 2013),
    ]),

    "true_story": ("Based on a true story.", [
        ("Schindler's List", 1993), ("12 Years a Slave", 2013), ("The Social Network", 2010),
        ("Catch Me If You Can", 2002), ("The Wolf of Wall Street", 2013), ("Spotlight", 2015),
        ("The Imitation Game", 2014), ("A Beautiful Mind", 2001), ("Goodfellas", 1990), ("Argo", 2012),
        ("The Pianist", 2002), ("Hidden Figures", 2016), ("Apollo 13", 1995), ("Moneyball", 2011),
        ("Erin Brockovich", 2000), ("The Big Short", 2015), ("Ford v Ferrari", 2019), ("Sully", 2016),
        ("The Theory of Everything", 2014), ("Dallas Buyers Club", 2013), ("127 Hours", 2010),
    ]),

    "heists": ("A great heist movie.", [
        ("Heat", 1995), ("Ocean's Eleven", 2001), ("The Italian Job", 2003), ("Inside Man", 2006),
        ("Baby Driver", 2017), ("The Town", 2010), ("Reservoir Dogs", 1992), ("Now You See Me", 2013),
        ("Logan Lucky", 2017), ("Den of Thieves", 2018), ("Widows", 2018), ("The Bank Job", 2008),
        ("Sexy Beast", 2000), ("Dog Day Afternoon", 1975), ("Thief", 1981), ("Ronin", 1998),
        ("Hell or High Water", 2016), ("Triple Frontier", 2019),
    ]),

    "mafia": ("Mafia & organized crime.", [
        ("The Godfather", 1972), ("The Godfather Part II", 1974), ("Goodfellas", 1990),
        ("The Irishman", 2019), ("Casino", 1995), ("Scarface", 1983), ("Once Upon a Time in America", 1984),
        ("The Departed", 2006), ("A Bronx Tale", 1993), ("Donnie Brasco", 1997), ("Road to Perdition", 2002),
        ("The Untouchables", 1987), ("Gomorrah", 2008), ("Eastern Promises", 2007), ("Carlito's Way", 1993),
        ("American Gangster", 2007), ("Mean Streets", 1973),
    ]),

    "serial_killers": ("Serial-killer thrillers.", [
        ("Se7en", 1995), ("The Silence of the Lambs", 1991), ("Zodiac", 2007), ("American Psycho", 2000),
        ("Memories of Murder", 2003), ("Psycho", 1960), ("No Country for Old Men", 2007),
        ("The Girl with the Dragon Tattoo", 2011), ("Monster", 2003), ("Manhunter", 1986),
        ("Copycat", 1995), ("The Bone Collector", 1999), ("Wind River", 2017), ("Prisoners", 2013),
        ("I Saw the Devil", 2010), ("Henry: Portrait of a Serial Killer", 1986),
    ]),

    "survival": ("Survival against the odds.", [
        ("The Revenant", 2015), ("Cast Away", 2000), ("127 Hours", 2010), ("All Is Lost", 2013),
        ("The Grey", 2011), ("Life of Pi", 2012), ("Gravity", 2013), ("The Martian", 2015),
        ("Alive", 1993), ("Into the Wild", 2007), ("Everest", 2015), ("The Impossible", 2012),
        ("Buried", 2010), ("Arctic", 2018), ("Adrift", 2018), ("Touching the Void", 2003),
        ("Society of the Snow", 2023), ("Jungle", 2017),
    ]),

    "apocalypse": ("The end of the world.", [
        ("Mad Max: Fury Road", 2015), ("Children of Men", 2006), ("The Road", 2009), ("28 Days Later", 2002),
        ("I Am Legend", 2007), ("World War Z", 2013), ("A Quiet Place", 2018), ("Snowpiercer", 2013),
        ("The Book of Eli", 2010), ("WALL-E", 2008), ("Train to Busan", 2016), ("12 Monkeys", 1995),
        ("Contagion", 2011), ("The Mist", 2007), ("Bird Box", 2018), ("Zombieland", 2009),
        ("Dawn of the Dead", 2004), ("Don't Look Up", 2021),
    ]),

    "female_leads": ("A fierce female lead.", [
        ("Mad Max: Fury Road", 2015), ("Alien", 1979), ("Kill Bill: Vol. 1", 2003), ("Wonder Woman", 2017),
        ("The Hunger Games", 2012), ("Gravity", 2013), ("Erin Brockovich", 2000), ("Hidden Figures", 2016),
        ("Promising Young Woman", 2020), ("Arrival", 2016), ("Atomic Blonde", 2017), ("Captain Marvel", 2019),
        ("Hanna", 2011), ("La Femme Nikita", 1990), ("Furiosa: A Mad Max Saga", 2024), ("Moana", 2016),
        ("Brave", 2012), ("Black Widow", 2021),
    ]),

    "hidden_gems": ("An underrated hidden gem.", [
        ("The Fall", 2006), ("Coherence", 2013), ("Moon", 2009), ("Primer", 2004), ("The Man from Earth", 2007),
        ("In Bruges", 2008), ("Nightcrawler", 2014), ("Ex Machina", 2014), ("A Ghost Story", 2017),
        ("The Handmaiden", 2016), ("Enemy", 2013), ("Sing Street", 2016), ("Under the Skin", 2013),
        ("Blue Ruin", 2013), ("The Nice Guys", 2016), ("Warrior", 2011), ("Locke", 2013),
        ("Hunt for the Wilderpeople", 2016), ("The Guilty", 2018), ("Upgrade", 2018),
    ]),

    "happy_ending": ("A feel-good happy ending.", [
        ("Forrest Gump", 1994), ("The Pursuit of Happyness", 2006), ("Slumdog Millionaire", 2008),
        ("Amélie", 2001), ("The Shawshank Redemption", 1994), ("Little Miss Sunshine", 2006),
        ("Paddington 2", 2017), ("The Intouchables", 2011), ("About Time", 2013), ("Big Fish", 2003),
        ("Up", 2009), ("Coco", 2017), ("Soul", 2020), ("Mamma Mia!", 2008), ("The Princess Bride", 1987),
        ("School of Rock", 2003), ("Sing Street", 2016), ("Chef", 2014), ("The Greatest Showman", 2017),
    ]),

    "brilliant_villains": ("An unforgettable villain.", [
        ("The Dark Knight", 2008), ("No Country for Old Men", 2007), ("The Silence of the Lambs", 1991),
        ("There Will Be Blood", 2007), ("Se7en", 1995), ("Django Unchained", 2012),
        ("Inglourious Basterds", 2009), ("Nightcrawler", 2014), ("Joker", 2019), ("Whiplash", 2014),
        ("Gladiator", 2000), ("Léon: The Professional", 1994), ("Misery", 1990), ("American Psycho", 2000),
        ("Oldboy", 2003), ("The Usual Suspects", 1995), ("Fargo", 1996),
        ("Star Wars: The Empire Strikes Back", 1980),
    ]),

    "dreamlike": ("Dreamlike & surreal.", [
        ("Mulholland Drive", 2001), ("Eternal Sunshine of the Spotless Mind", 2004), ("Paprika", 2006),
        ("The Fall", 2006), ("Spirited Away", 2001), ("Enter the Void", 2009), ("The Science of Sleep", 2006),
        ("Pan's Labyrinth", 2006), ("Under the Skin", 2013), ("The Tree of Life", 2011),
        ("Blade Runner 2049", 2017), ("Amélie", 2001), ("A Ghost Story", 2017), ("Melancholia", 2011),
        ("Waking Life", 2001), ("Solaris", 1972), ("Annihilation", 2018), ("The Cell", 2000),
    ]),

    "wholesome": ("Warm & wholesome.", [
        ("Paddington 2", 2017), ("My Neighbor Totoro", 1988), ("Kiki's Delivery Service", 1989),
        ("Up", 2009), ("Coco", 2017), ("Ratatouille", 2007), ("Big Hero 6", 2014), ("Klaus", 2019),
        ("The Secret Life of Walter Mitty", 2013), ("About Time", 2013), ("Chef", 2014),
        ("Hunt for the Wilderpeople", 2016), ("Wonder", 2017), ("A Man Called Otto", 2022),
        ("Ponyo", 2008), ("The Peanut Butter Falcon", 2019), ("Soul", 2020), ("Sing Street", 2016),
    ]),

    "mind_bending": ("Mind-bending & twisty.", [
        ("Inception", 2010), ("Memento", 2000), ("Primer", 2004), ("Predestination", 2014),
        ("Coherence", 2013), ("Donnie Darko", 2001), ("Mulholland Drive", 2001), ("Shutter Island", 2010),
        ("The Prestige", 2006), ("Tenet", 2020), ("Enemy", 2013), ("Triangle", 2009), ("Timecrimes", 2007),
        ("The Matrix", 1999), ("Eternal Sunshine of the Spotless Mind", 2004), ("Source Code", 2011),
        ("Arrival", 2016), ("12 Monkeys", 1995), ("Being John Malkovich", 1999), ("Us", 2019),
    ]),
}


async def _resolve(title: str, year: int):
    try:
        hits = await tmdb.search(title, "movie", count=6)
    except Exception:
        return None
    if not hits:
        return None

    def score(h):
        s = 0.0
        hy = h.get("year")
        if hy == str(year):
            s += 3
        elif hy and abs(int(hy) - year) <= 1:
            s += 1.5
        if (h.get("title") or "").lower() == title.lower():
            s += 2
        s += min(h.get("vote_count", 0), 5000) / 5000.0
        return s

    hits.sort(key=score, reverse=True)
    return hits[0]


@router.get("/collection")
async def collection(id: str = Query(..., description="Collection id (matches the mood card id)")):
    entry = COLLECTIONS.get(id)
    if not entry:
        raise HTTPException(status_code=404, detail="Unknown collection.")
    why, items = entry

    key = f"collection::{id}"
    cached = await cache.get(key)
    if cached is not None:
        return cached

    resolved = await asyncio.gather(*[_resolve(t, y) for t, y in items])
    seen, movies = set(), []
    for m in resolved:
        if m and m["id"] not in seen:
            seen.add(m["id"])
            movies.append(m)
    movies = movies[:LIMIT]

    try:
        await tmdb.attach_providers(movies)
    except Exception:
        pass

    picks = [
        Pick(tmdb_id=m["id"], title=m["title"], year=m["year"], overview=m["overview"],
             poster_url=m["poster_url"], rating=m["rating"], why=why,
             providers=m.get("providers", []))
        for m in movies
    ]
    result = {"picks": [p.model_dump() for p in picks]}
    await cache.set(key, result)
    return result
