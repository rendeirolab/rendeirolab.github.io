for F in *.jpg; do magick convert ${F} -quality 95 ${F/.jpg/.webp}; done
for F in *.jpeg; do magick convert ${F} -quality 95 ${F/.jpeg/.webp}; done
