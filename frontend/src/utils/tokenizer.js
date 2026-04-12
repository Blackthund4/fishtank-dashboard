const WORD_RE = /[a-z]{3,}/g

const STOPWORDS = new Set([
    // Standard English
    "the", "and", "that", "have", "for", "not", "with", "you", "this",
    "but", "his", "from", "they", "been", "one", "had", "has", "her",
    "all", "their", "there", "what", "about", "which", "when", "will",
    "each", "make", "like", "just", "over", "such", "than", "them",
    "very", "some", "can", "would", "could", "into", "other", "more",
    "should", "was", "were", "are", "did", "does", "how", "its",
    "she", "him", "who", "get", "got", "out", "also", "your",
    "only", "then", "too", "any", "don", "now", "way", "may",
    "own", "say", "said", "most", "same", "being", "because",
    "going", "want", "still", "even", "think", "know", "come",
    "really", "right", "off", "back", "need", "ever", "put",
    "good", "why", "time", "bet", "nothing", "without", "day",
    "days", "man",
    // Chat noise
    "lol", "lmao", "lmfao", "rofl", "haha", "hahaha", "hehe",
    "bro", "bruh", "dude", "guys", "yeah", "yea", "nah",
    "omg", "omfg", "wtf", "idk", "imo", "tbh", "tho",
    "pog", "poggers", "kek", "kekw", "copium", "cope",
    "based", "ratio", "btw", "please",
    "http", "https", "www", "com",
    // Bot/game command spam
    "coinflip", "double", "lexxpoints",
    // Platform noise
    "clip", "tip", "sfx", "tts", "used",
])

export function tokenize(text) {
    if (!text || typeof text !== 'string') return []
    const words = text.toLowerCase().match(WORD_RE) || []
    return words.filter(w => !STOPWORDS.has(w))
}
