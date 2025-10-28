for F in *.jpg; do convert ${F} -quality 95 ${F/.jpg/.webp}; done
