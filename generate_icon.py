from PIL import Image, ImageDraw


def create_app_icon(output_ico_path: str = "app_icon.ico"):
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Zaoblené tmavě modré pozadí
    draw.rounded_rectangle(
        [(12, 12), (size - 12, size - 12)],
        radius=48,
        fill=(17, 24, 39, 255),
        outline=(59, 130, 246, 255),
        width=8,
    )

    # Zvukové vlny (ekvalizér + play symbol)
    bars = [
        (56, 96, 160, (96, 165, 250, 255)),
        (88, 64, 192, (59, 130, 246, 255)),
        (120, 44, 212, (139, 92, 246, 255)),
        (152, 72, 184, (59, 130, 246, 255)),
        (184, 100, 156, (16, 185, 129, 255)),
    ]
    for x, top, bottom, color in bars:
        draw.rounded_rectangle([(x, top), (x + 18, bottom)], radius=9, fill=color)

    img.save(output_ico_path, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])


if __name__ == "__main__":
    create_app_icon()
