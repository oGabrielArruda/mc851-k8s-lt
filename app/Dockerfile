FROM golang:1.27-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY *.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /fib-server .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /fib-server /fib-server
EXPOSE 8080
USER nonroot:nonroot
ENTRYPOINT ["/fib-server"]
