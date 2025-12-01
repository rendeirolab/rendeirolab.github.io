for F in *.png; do magick ${F} -quality 95 ${F/.png/.webp}; done
for F in *.jpg; do magick ${F} -quality 95 ${F/.jpg/.webp}; done
for F in *.jpeg; do magick ${F} -quality 95 ${F/.jpeg/.webp}; done
