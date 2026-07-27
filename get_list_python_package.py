import requests
import json


OUTPUT = r".\requirements.txt"


# PyPI官方简单索引
URL = "https://hugovk.github.io/top-pypi-packages/top-pypi-packages-30-days.json"


print("正在获取热门包列表...")


data = requests.get(
    URL,
    timeout=30
).json()


packages = []


for item in data["rows"]:

    name = item["project"]

    packages.append(name)


# 取前5000
packages = packages[:20000]


with open(
    OUTPUT,
    "w",
    encoding="utf-8"
) as f:

    for p in packages:
        f.write(
            p+"\n"
        )


print(
    "生成完成:",
    OUTPUT
)

print(
    "数量:",
    len(packages)
)