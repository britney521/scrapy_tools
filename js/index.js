o = {
    "url": "/rest/v/search/feed",
    "query": {
        "caver": "2"
    },
    "form": {},
    "requestBody": {
        "keyword": "乌拉",
        "pcursor": "2",
        "searchSessionId": "MTRfNTQxNjYzNzU4N18xNzg5MTc5MzczMzI0X-S5jOaLiV83OTMz"
    }
}
const {signResult: a} = await getSig4(e, o);